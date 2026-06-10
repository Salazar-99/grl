import re
import urllib.request

import yaml

from vms.versions import MAP_REPO_TO_ENV_YML_PATHS

CONDA_TO_PIP = {"matplotlib-base": "matplotlib"}
SKIP = {
    "pip",
    "pygobject",
    "wxpython",
    "pyqt",
    "hdf5",
    "cdms2",
    "iris",
    "pynio",
    "nc-time-axis",
}


def fetch_env_yml(repo: str, commit: str) -> str:
    for path in MAP_REPO_TO_ENV_YML_PATHS[repo]:
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
        with urllib.request.urlopen(url) as resp:
            if resp.status == 200:
                return resp.read().decode()
    raise ValueError(f"no environment.yml for {repo}@{commit}")


def packages_from_env_yml(text: str) -> list[str]:
    data = yaml.safe_load(text)
    pkgs: list[str] = []
    for dep in data.get("dependencies", []):
        if isinstance(dep, dict) and "pip" in dep:
            pkgs.extend(dep["pip"])
        elif isinstance(dep, str):
            name = re.split(r"[<>=!]", dep)[0].strip()
            if name in SKIP:
                continue
            pkgs.append(CONDA_TO_PIP.get(name, dep))
    return pkgs
