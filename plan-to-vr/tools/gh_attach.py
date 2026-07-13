#!/usr/bin/env python3
"""Upload a file to GitHub as a real user-attachment (native inline player).

GitHub renders an inline <video>/<img> player only for files uploaded
through its web-composer attachment pipeline (github.com/user-attachments
/assets/...). That pipeline is NOT in the REST/GraphQL API and rejects
PATs - it authenticates with a logged-in web SESSION cookie. So VR-note
recordings can become native attachments (not committed repo blobs) only
by replaying that browser flow with a session cookie.

To keep the credential low-stakes, use a throwaway BOT account added as a
write collaborator on the repo (not your personal account): if the cookie
leaks, the blast radius is write access to this one public repo.

The bot's `user_session` cookie is read from the GH_ATTACH_COOKIE env var
(set it in the Claude Code environment settings so it survives container
restarts). Cookies expire ~2 weeks; `check` reports that loudly and the
loop backfills once refreshed.

Subcommands:
    check                       validate the cookie + write access; exit 0 OK,
                                3 = expired/no access, 1 = other error
    upload <owner/repo> <file>  upload; print the user-attachments URL to
                                stdout; exit 3 if the cookie is dead

This replays an UNDOCUMENTED GitHub flow that can change without notice;
every step fails loud with a distinct exit code so a break is obvious.
"""
import os
import re
import sys
import json
import mimetypes
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
EXIT_EXPIRED = 3


def _cookie():
    c = os.environ.get("GH_ATTACH_COOKIE", "").strip()
    if not c:
        sys.stderr.write(
            "GH_ATTACH_COOKIE is not set. Provision the bot cookie "
            "(see plan-to-vr/tools/README-attachments.md).\n")
        sys.exit(1)
    # accept either a bare user_session value or a full "a=b; c=d" string
    return c if "=" in c else f"user_session={c}"


def _req(url, data=None, headers=None, method=None):
    h = {"User-Agent": UA, "Cookie": _cookie()}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    return urllib.request.urlopen(r, timeout=30)


def _fetch_upload_ctx(owner, repo):
    """From a page with an attachment-capable comment box (the new-issue
    page) scrape BOTH the CSRF token GitHub scopes to /upload/policies/
    assets and the numeric repository_id. Uses only github.com (the egress
    proxy allows github.com but blocks api.github.com). A dead cookie
    redirects to /login, which we surface as expired.
    Returns (authenticity_token, repository_id)."""
    url = f"https://github.com/{owner}/{repo}/issues/new"
    try:
        resp = _req(url, headers={"Accept": "text/html"})
        html = resp.read().decode("utf-8", "replace")
        final = resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            sys.stderr.write(f"EXPIRED: repo page returned {e.code}\n")
            sys.exit(EXIT_EXPIRED)
        raise
    if "/login" in final or "session-authentication" in html:
        sys.stderr.write("EXPIRED: redirected to login (cookie dead)\n")
        sys.exit(EXIT_EXPIRED)
    # the upload form carries a hidden authenticity_token; the upload-scoped
    # one sits on the element wired to the policies URL
    m = re.search(
        r'class="[^"]*js-data-upload-policy-url-csrf[^"]*"[^>]*'
        r'value="([^"]+)"', html)
    if not m:
        m = re.search(
            r'name="authenticity_token"\s+value="([^"]+)"[^>]*'
            r'(?:data-upload|upload-policies)', html)
    if not m:
        m = re.search(r'name="authenticity_token"\s+value="([^"]+)"', html)
    if not m:
        sys.stderr.write("could not find an upload CSRF token on the page "
                         "(GitHub markup may have changed)\n")
        sys.exit(1)
    token = m.group(1)
    rid = re.search(
        r'octolytics-dimension-repository_id"\s+content="(\d+)"', html) \
        or re.search(r'"repository_id":(\d+)', html) \
        or re.search(r'data-upload-repository-id="(\d+)"', html)
    if not rid:
        sys.stderr.write("could not find repository_id on the page\n")
        sys.exit(1)
    return token, int(rid.group(1))


def _who(owner, repo):
    """A logged-in whoami via the cookie, best-effort, for `check`."""
    try:
        resp = _req("https://github.com/settings/profile",
                    headers={"Accept": "text/html"})
        if "/login" in resp.geturl():
            return None
        html = resp.read().decode("utf-8", "replace")
        m = re.search(r'meta name="user-login" content="([^"]+)"', html)
        return m.group(1) if m else "?"
    except urllib.error.HTTPError:
        return None


def _multipart(fields, files):
    boundary = "----plan2vrBoundary7MA4YWxkTrZu0gW"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{k}"\r\n\r\n{v}\r\n').encode()
    for k, (fname, content, ctype) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="{k}"; filename="{fname}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def upload(owner, repo, path):
    token, repo_id = _fetch_upload_ctx(owner, repo)
    data = open(path, "rb").read()
    name = os.path.basename(path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"

    # 1. ask GitHub for an upload policy (S3 presigned form)
    body, cty = _multipart(
        {"name": name, "size": str(len(data)), "content_type": ctype,
         "authenticity_token": token, "repository_id": str(repo_id)}, {})
    try:
        resp = _req("https://github.com/upload/policies/assets", data=body,
                    headers={"Content-Type": cty,
                             "Accept": "application/json",
                             "Origin": "https://github.com",
                             "X-Requested-With": "XMLHttpRequest"},
                    method="POST")
        policy = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.stderr.write(f"EXPIRED: policy request {e.code}\n")
            sys.exit(EXIT_EXPIRED)
        sys.stderr.write(f"policy request failed {e.code}: "
                         f"{e.read()[:300]!r}\n")
        sys.exit(1)

    # 2. POST the bytes to the presigned storage endpoint
    up_url = policy["upload_url"]
    form = policy.get("form") or policy.get("asset", {}).get("form") or {}
    body, cty = _multipart(dict(form), {"file": (name, data, ctype)})
    req = urllib.request.Request(
        up_url, data=body,
        headers={"User-Agent": UA, "Content-Type": cty}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status not in (200, 201, 204):
            sys.stderr.write(f"storage upload failed: {r.status}\n")
            sys.exit(1)

    # 3. finalize the asset with GitHub
    up_auth = policy.get("asset_upload_url")
    up_tok = policy.get("asset_upload_authenticity_token")
    if up_auth and up_tok:
        body, cty = _multipart({"authenticity_token": up_tok}, {})
        try:
            _req("https://github.com" + up_auth, data=body,
                 headers={"Content-Type": cty,
                          "Accept": "application/json"}, method="PUT")
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"finalize warning {e.code} (asset may still "
                             f"be usable)\n")

    href = policy.get("asset", {}).get("href")
    if not href:
        sys.stderr.write(f"no asset href in policy response: {policy!r}\n")
        sys.exit(1)
    print(href)
    return href


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "check":
        owner, repo = (sys.argv[2] if len(sys.argv) > 2
                       else "onlinegeek101/vrmatics").split("/")
        who = _who(owner, repo)
        if not who:
            print("EXPIRED: cookie is not a live session")
            sys.exit(EXIT_EXPIRED)
        _fetch_upload_ctx(owner, repo)   # also proves write access
        print(f"OK as {who} (write access to {owner}/{repo} confirmed)")
    elif cmd == "upload":
        owner, repo = sys.argv[2].split("/")
        upload(owner, repo, sys.argv[3])
    else:
        sys.stderr.write(f"unknown command: {cmd}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
