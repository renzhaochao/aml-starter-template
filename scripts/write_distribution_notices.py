"""Collect installed runtime license texts into the Windows distribution."""
from importlib.metadata import distribution
from pathlib import Path
import sys


PACKAGES = (
    "fastapi", "starlette", "pydantic", "pydantic_core", "annotated-types",
    "annotated-doc", "typing_extensions", "typing-inspection", "anyio", "idna",
    "uvicorn", "h11", "click", "colorama", "jieba", "python-dotenv",
    "opentelemetry-api", "pyinstaller", "pyinstaller-hooks-contrib",
)


def main():
    sections = ["AML Memory Server - third-party notices\n"]
    for name in PACKAGES:
        package = distribution(name)
        sections.append(f"\n{'=' * 72}\n{name} {package.version}\n")
        for field in ("License-Expression", "License", "Home-page"):
            if package.metadata.get(field):
                sections.append(f"{field}: {package.metadata[field]}\n")
        for item in package.files or []:
            if any(part.lower().startswith(("license", "copying", "notice")) for part in item.parts):
                path = Path(package.locate_file(item))
                if path.is_file() and path.suffix.lower() not in {".py", ".pyc"}:
                    sections.append(f"\n--- {item} ---\n{path.read_text(encoding='utf-8', errors='replace')}\n")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.exists():
        raise FileNotFoundError(f"Python license not found: {python_license}")
    sections.append("\nPython runtime\n" + python_license.read_text(encoding="utf-8"))
    (Path(sys.argv[1]) / "THIRD-PARTY-NOTICES.txt").write_text("".join(sections), encoding="utf-8")


if __name__ == "__main__":
    main()
