import re
import urllib.error
import urllib.request

from vms.versions import MAP_REPO_TO_REQS_PATHS

REPLACE_REQ_PACKAGES = [
    ("types-pkg_resources", "types-setuptools"),
]


def clean_requirements(text: str) -> str:
    for pkg, replacement in REPLACE_REQ_PACKAGES:
        text = re.sub(
            rf"^{re.escape(pkg)}([<>=!~]=?.*|$)",
            replacement,
            text,
            flags=re.MULTILINE,
        )
    return text


def _fetch_url(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError:
        return None


def fetch_requirements(repo: str, commit: str) -> str:
    req_paths = MAP_REPO_TO_REQS_PATHS.get(repo)
    if not req_paths:
        raise ValueError(f"no requirements path for {repo}")

    for req_path in req_paths:
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{req_path}"
        text = _fetch_url(url)
        if text is not None:
            break
    else:
        raise ValueError(f"no requirements file for {repo}@{commit}")

    req_dir = "/".join(req_path.split("/")[:-1])
    lines: list[str] = []
    extra: list[str] = []

    def skip(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith(("-e .", "#", ".[test"))

    for line in text.splitlines():
        if line.strip().startswith("-r"):
            included = line[len("-r") :].strip()
            prefix = f"{req_dir}/" if req_dir else ""
            included_url = (
                f"https://raw.githubusercontent.com/{repo}/{commit}/{prefix}{included}"
            )
            included_text = _fetch_url(included_url)
            if included_text is not None:
                for included_line in included_text.splitlines():
                    if not skip(included_line):
                        extra.append(included_line)
        elif not skip(line):
            lines.append(line)

    extra.append("\n".join(lines))
    return clean_requirements("\n".join(extra))
