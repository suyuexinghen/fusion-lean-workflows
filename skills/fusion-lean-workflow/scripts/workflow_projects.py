"""Repository names accepted in immutable task evidence and review contracts."""

PROJECTS = ("fusion-lean", "fusion-lean-wiki", "fusion-lean-skills", "fusion-compute")


def project_path(root, name):
    path = root / name
    if name == "fusion-compute" and not (path / ".git").exists():
        raise ValueError(f"missing fusion-compute Git checkout: {path}")
    return path
