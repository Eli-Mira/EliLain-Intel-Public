"""Store the OPTIONAL Netlify mirror secrets without printing either value.

Cloudflare Pages is the project's only host; the workflow no longer reads these
secrets. This remains only for manually restoring the best-effort mirror, and is
useless unless `NETLIFY_MIRROR_ENABLED=true` is also set on the workflow.

Reads explicitly supplied NETLIFY_AUTH_TOKEN, NETLIFY_SITE_ID and GH_REPO
environment variables, then pipes secret values into `gh secret set` on stdin.
Does not inspect any credential store or embed operator identifiers.
"""

import os

import subprocess
import sys

REPO = os.getenv("GH_REPO", "")
SITE_ID = os.getenv("NETLIFY_SITE_ID", "")


def gh_token() -> str:
    return os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN", "")


def main() -> int:
    netlify_token = os.getenv("NETLIFY_AUTH_TOKEN", "")
    if not netlify_token or not SITE_ID or not __import__("re").fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", REPO):
        raise SystemExit("Set GH_REPO, NETLIFY_AUTH_TOKEN and NETLIFY_SITE_ID explicitly")

    # Inherit the ambient environment (TLS/proxy/host settings) and only add the
    # GitHub credential; replacing env wholesale breaks gh's own connection.
    env = dict(os.environ)
    if gh_token():
        env["GH_TOKEN"] = gh_token()
    for name, value in (("NETLIFY_AUTH_TOKEN", netlify_token), ("NETLIFY_SITE_ID", SITE_ID)):
        done = subprocess.run(["gh", "secret", "set", name, "--repo", REPO],
                              input=value, text=True, capture_output=True, env=env)
        if done.returncode != 0:
            print(f"failed to set {name}; provider error withheld", file=sys.stderr)
            return 1
        # Print the name only, never the value.
        print(f"stored secret {name} (len {len(value)})")

    listing = subprocess.run(["gh", "secret", "list", "--repo", REPO],
                             capture_output=True, text=True, env=env)
    print(listing.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())