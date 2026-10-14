"""Resolve only explicitly declared, hash-bound SPICE includes into a bounded self-contained deck."""

import re
import shlex
from typing import Literal

from pydantic import Field

from .config import StrictModel, digest, local_path


class ModelSource(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str
    review_note: str = Field(min_length=20)


class ModelManifest(StrictModel):
    schema_version: Literal[1] = 1
    models: list[ModelSource] = Field(min_length=1)


def flatten(deck, manifest):
    base = deck.parent.resolve()
    registered = {local_path(base, m.path): m for m in manifest.models}
    if len(registered) != len(manifest.models):
        raise ValueError("Duplicate simulation model path")
    dependencies, used = {}, set()
    for path, model in registered.items():
        if digest(path) != model.sha256:
            raise ValueError(f"Model hash changed: {model.path}")
        dependencies[str(path)] = model.sha256
    total = 0

    def expand(path, chain):
        nonlocal total
        if path in chain or len(chain) > 8:
            raise ValueError("Cyclic or overly deep SPICE includes")
        text = path.read_text()
        total += len(text)
        if total > 10_000_000:
            raise ValueError("SPICE model source budget exceeded")
        lines = []
        for line in text.splitlines():
            match = re.match(r"^\s*\.inc(?:lude)?\s+(.*)$", line, re.I)
            if match:
                args = shlex.split(match[1])
                if len(args) != 1:
                    raise ValueError("Use one explicitly quoted SPICE include path")
                target = (path.parent / args[0]).resolve()
                if target not in registered:
                    raise ValueError("SPICE include is not declared in the reviewed model manifest")
                used.add(target)
                lines.extend(
                    [
                        "* BEGIN reviewed include: " + registered[target].path,
                        expand(target, chain + [path]),
                        "* END reviewed include",
                    ]
                )
            elif path != deck.resolve() and re.match(r"^\s*\.end\s*(?:$|\*)", line, re.I):
                raise ValueError("An included model must not terminate the parent circuit with .end")
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    text = expand(deck.resolve(), [])
    return (
        text,
        {str(p): dependencies[str(p)] for p in sorted(used)},
        [str(p) for p in registered if p not in used],
    )
