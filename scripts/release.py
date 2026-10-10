"""GitHub calls for the Shorts workflow (spec D6): draft release, assets, notes, feed tags.

Everything goes through `gh api` with GITHUB_TOKEN (GH_TOKEN) and GH_REPO from the
job's environment, so the workflow needs no extra action or token. Asset swaps
are atomic per name: upload `<name>.part`, delete the old `<name>`, rename the
part. A cancelled job leaves either the old asset or a `.part` that the next
`make.py plan` deletes, never a half-replaced video.

Library only; make.py plan and make.py release call it.
"""

import json
import os
import subprocess
import tempfile
import urllib.parse


class GitHubError(Exception):
    pass


def parse_stream(text):
    """`gh api --paginate` prints one JSON value per page, back to back."""
    dec = json.JSONDecoder()
    out, i = [], 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        val, i = dec.raw_decode(text, i)
        out.append(val)


class GitHub:
    def __init__(self, repo, gh="gh", cwd=None):
        self.repo = repo
        self.gh = gh
        self.cwd = cwd

    @classmethod
    def from_env(cls, cwd=None):
        repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not repo or not token:
            return None
        return cls(repo, cwd=cwd)

    def api(self, path, method=None, raw=(), typed=(), headers=(), input_path=None, paginate=False, out_path=None):
        cmd = [self.gh, "api"]
        if method:
            cmd += ["-X", method]
        for h in headers:
            cmd += ["-H", h]
        for k, v in raw:
            cmd += ["-f", f"{k}={v}"]
        for k, v in typed:
            cmd += ["-F", f"{k}={v}"]
        if input_path:
            cmd += ["--input", input_path]
        if paginate:
            cmd.append("--paginate")
        cmd.append(path)
        res = subprocess.run(cmd, capture_output=True, cwd=self.cwd)
        if res.returncode != 0:
            raise GitHubError(f"gh api {method or 'GET'} {path.split('?')[0]}: "
                              f"{res.stderr.decode(errors='replace').strip() or res.stdout.decode(errors='replace')[:300]}")
        if out_path:
            with open(out_path, "wb") as fh:
                fh.write(res.stdout)
            return None
        text = res.stdout.decode("utf-8")
        if not text.strip():
            return None
        vals = parse_stream(text)
        if paginate:
            flat = []
            for v in vals:
                flat += v if isinstance(v, list) else [v]
            return flat
        return vals[0]

    # ---------------------------------------------------------- releases

    def releases(self):
        return self.api(f"repos/{self.repo}/releases?per_page=100", paginate=True) or []

    def find_release(self, tag):
        for r in self.releases():
            if r.get("tag_name") == tag and r.get("draft"):
                return r
        return None

    def find_or_create_release(self, tag, name, target=""):
        rel = self.find_release(tag)
        if rel:
            return rel
        raw = [("tag_name", tag), ("name", name)]
        if target:
            raw.append(("target_commitish", target))
        return self.api(f"repos/{self.repo}/releases", method="POST", raw=raw, typed=[("draft", "true")])

    def set_notes(self, release_id, body):
        fd, tmp = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"body": body}, fh)
            return self.api(f"repos/{self.repo}/releases/{release_id}", method="PATCH", input_path=tmp)
        finally:
            os.remove(tmp)

    # ---------------------------------------------------------- assets

    def assets(self, release_id):
        return self.api(f"repos/{self.repo}/releases/{release_id}/assets?per_page=100", paginate=True) or []

    def upload_asset(self, release_id, path, name, label, mime):
        q = urllib.parse.urlencode({"name": name, "label": label})
        return self.api(f"https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets?{q}",
                        method="POST", headers=[f"Content-Type: {mime}"], input_path=path)

    def rename_asset(self, asset_id, name, label):
        return self.api(f"repos/{self.repo}/releases/assets/{asset_id}", method="PATCH",
                        raw=[("name", name), ("label", label)])

    def delete_asset(self, asset_id):
        return self.api(f"repos/{self.repo}/releases/assets/{asset_id}", method="DELETE")

    def download_asset(self, asset_id, dest):
        return self.api(f"repos/{self.repo}/releases/assets/{asset_id}", headers=["Accept: application/octet-stream"],
                        out_path=dest)

    # ---------------------------------------------------------- tags

    def remote_tag_exists(self, tag):
        res = subprocess.run(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], capture_output=True,
                             text=True, cwd=self.cwd)
        if res.returncode != 0:
            raise GitHubError(f"git ls-remote: {res.stderr.strip()}")
        return bool(res.stdout.strip())

    def create_tag(self, tag, sha):
        return self.api(f"repos/{self.repo}/git/refs", method="POST", raw=[("ref", f"refs/tags/{tag}"), ("sha", sha)])


def swap_upload(gh, release_id, assets, path, name, label, mime):
    """Replace asset `name` so that at every moment the release holds the old file, the new one, or a .part."""
    part = name + ".part"
    if part in assets:
        gh.delete_asset(assets.pop(part)["id"])
    new = gh.upload_asset(release_id, path, part, label, mime)
    old = assets.pop(name, None)
    if old:
        gh.delete_asset(old["id"])
    done = gh.rename_asset(new["id"], name, label) or dict(new, name=name, label=label)
    assets[name] = done
    return done
