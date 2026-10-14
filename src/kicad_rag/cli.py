from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .checks import Design, evaluate, load_design
from .config import Document, Project, Settings, atomic_json, digest, load_project, local_path, save_project
from .kicad import Graph, export_schematic, kicad_cli
from .rag import ask, render
from .store import Store, index_project


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def parser():
    p = argparse.ArgumentParser(description="KiCad project datasheet assistant: retrieve, cite, and review component interactions.")
    p.add_argument("--project", type=Path, default=Path.cwd(), help="Project folder containing project.json")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create an isolated project folder")
    init.add_argument("path", type=Path)
    init.add_argument("--name")
    add = sub.add_parser("add", help="Copy a datasheet/note into this project and register its exact parts")
    add.add_argument("file", type=Path)
    add.add_argument("--id", required=True)
    add.add_argument("--parts", nargs="+", default=[])
    add.add_argument("--title")
    add.add_argument("--revision", default="unspecified")
    add.add_argument("--kind", choices=["datasheet", "application_note", "errata", "design_note"], default="datasheet")
    add.add_argument("--source-url")
    remove = sub.add_parser("remove", help="Remove a document from the next index; preserve its file")
    remove.add_argument("id")
    sync = sub.add_parser("sync-kicad", help="Export an actual schematic through KiCad into XML")
    sync.add_argument("schematic", type=Path, nargs="?")
    imp = sub.add_parser("import-netlist", help="Import an existing KiCad XML netlist snapshot")
    imp.add_argument("file", type=Path)
    mapping = sub.add_parser("map-part", help="Map a component reference to its exact manufacturer part number")
    mapping.add_argument("ref")
    mapping.add_argument("part")
    configure = sub.add_parser("configure", help="Select an embedding backend and/or answer backend")
    configure.add_argument("--embedding-provider", choices=["none", "ollama", "openai", "local"])
    configure.add_argument("--embedding-model")
    configure.add_argument("--chat-provider", choices=["none", "ollama", "openai"])
    configure.add_argument("--chat-model")
    configure.add_argument("--ollama-url")
    configure.add_argument("--embedding-query-prefix")
    configure.add_argument("--embedding-document-prefix")
    configure.add_argument("--reranker-model")
    configure.add_argument("--shared-extraction-cache")
    configure.add_argument("--ocr-mode", choices=["off", "auto", "always"])
    configure.add_argument("--ocr-language")
    configure.add_argument("--ocr-dpi", type=int)
    configure.add_argument("--restore-parent-context", action=argparse.BooleanOptionalAction, default=None)
    index = sub.add_parser("index", help="Extract documents and atomically update keyword/vector index")
    index.add_argument("--force", action="store_true", help="Re-extract and re-embed every document")
    for cmd in ("ask", "search"):
        query = sub.add_parser(cmd, help="Answer with citations" if cmd == "ask" else "Retrieve evidence without generation")
        query.add_argument("question")
        query.add_argument("--refs", nargs="+", default=[])
        query.add_argument("--parts", nargs="+", help="Provisional exact parts before a schematic exists")
        query.add_argument("--lexical-only", action="store_true", help="Bypass query embedding calls")
        query.add_argument("--evidence-only", action="store_true")
        query.add_argument("--json", action="store_true")
    sub.add_parser("status", help="Show project setup, indexing state and extraction warnings")
    sub.add_parser("coverage", help="List components lacking mapped datasheets")
    sub.add_parser("check", help="Run reviewed numerical constraints from design.json")
    sub.add_parser("doctor", help="Check runtime and KiCad availability, without contacting model APIs")
    sub.add_parser("gui", help="Open the local desktop question/evidence interface")
    page = sub.add_parser("page", help="Read the exact indexed page or render the original PDF page")
    page.add_argument("document")
    page.add_argument("number", type=int)
    page.add_argument("--render", type=Path, help="Save original PDF page as PNG")
    path = sub.add_parser("path", help="Show a topological path, not a signal/causal path")
    path.add_argument("start")
    path.add_argument("end")
    schema = sub.add_parser("schema", help="Export JSON schema for project.json or design.json")
    schema.add_argument("kind", choices=["project", "design", "evidence-request", "parameter", "schematic-spec", "intent", "hierarchy-spec", "board-spec", "inspection"])
    evidence = sub.add_parser("evidence", help="Resolve exact engineering obligations from accepted parameter facts")
    evidence.add_argument("--request", type=Path, required=True)
    evidence.add_argument("--output", type=Path, required=True)
    evidence.add_argument("--lexical-only", action="store_true")
    verify = sub.add_parser("verify-evidence", help="Reject changed or incomplete evidence packets")
    verify.add_argument("packet", type=Path)
    calc = sub.add_parser("calculate", help="Run existing calculators using accepted evidence packet inputs")
    calc.add_argument("--evidence", type=Path, required=True)
    calc.add_argument("--design", type=Path, required=True)
    calc.add_argument("--output", type=Path, required=True)
    sub.add_parser("impact", help="Find changed source/review dependencies")
    review = sub.add_parser("accept-fact", help="Record a completed original-page review; never use without inspecting the source")
    review.add_argument("id")
    review.add_argument("--actor", required=True)
    review.add_argument("--rationale", required=True)
    hierarchy = sub.add_parser("generate-hierarchy", help="Generate wired parent/child sheets with real hierarchical ports")
    hierarchy.add_argument("--spec", type=Path, required=True)
    hierarchy.add_argument("--intent", type=Path, required=True)
    hierarchy.add_argument("--output", type=Path, required=True)
    generate = sub.add_parser("generate-schematic", help="Build and natively verify a new wired schematic from separate geometry and intent")
    generate.add_argument("--spec", type=Path, required=True)
    generate.add_argument("--intent", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    board = sub.add_parser("generate-board", help="Create an explicitly placed/routed multilayer KiCad 10 board")
    board.add_argument("schematic", type=Path)
    board.add_argument("--spec", type=Path, required=True)
    board.add_argument("--intent", type=Path, required=True)
    release = sub.add_parser('release', help='Record a native release after fresh checks and actual recorded inspections')
    release.add_argument('schematic', type=Path)
    release.add_argument('--intent', type=Path, required=True)
    release.add_argument('--visual-review', type=Path, required=True)
    release.add_argument('--board', type=Path)
    release.add_argument('--physical-review', type=Path)
    extraction = sub.add_parser("extract-parameters", help="Propose source-bound numeric records for original-page review")
    extraction.add_argument("--request", type=Path, required=True)
    extraction.add_argument("--output", type=Path, required=True)
    extraction.add_argument("--lexical-only", action="store_true")
    ocr = sub.add_parser('ocr-copy', help='OCR a new derived PDF while retaining original source bytes and lineage')
    ocr.add_argument('source', type=Path)
    ocr.add_argument('output', type=Path)
    diff = sub.add_parser('source-diff', help='Compare original document revisions by extracted page text and tables')
    diff.add_argument('previous', type=Path)
    diff.add_argument('current', type=Path)
    diff.add_argument('--output', type=Path, required=True)
    sim = sub.add_parser('simulate', help='Execute a reviewed self-contained ngspice deck with bounded resources')
    sim.add_argument('deck', type=Path)
    sim.add_argument('--output', type=Path, required=True)
    sim.add_argument('--models', type=Path, help='Reviewed manifest of hash-bound SPICE include files')
    manufacture = sub.add_parser('export-manufacturing', help='Export a checked two-layer board; output review remains required')
    manufacture.add_argument('board', type=Path)
    manufacture.add_argument('--output', type=Path, required=True)
    assembly = sub.add_parser("export-assembly", help="Export a default-variant BOM and native placement CSV")
    assembly.add_argument("board", type=Path)
    assembly.add_argument("--output", type=Path, required=True)
    native = sub.add_parser("verify-native", help="Export, compare independent connectivity, ERC and render all schematic pages")
    native.add_argument("schematic", type=Path)
    native.add_argument("--intent", type=Path, required=True)
    native.add_argument("--output", type=Path, required=True)
    native.add_argument("--board", type=Path)
    native.add_argument("--refill", action="store_true")
    return p


def run(args):
    root = args.project.expanduser().resolve()
    if args.command == "init":
        root = args.path.expanduser().resolve()
        if (root / "project.json").exists():
            raise ValueError("Project already exists; choose another folder")
        project = Project(name=args.name or root.name)
        save_project(root, project)
        (root / "datasheets").mkdir(exist_ok=True)
        if not (root / "design.json").exists():
            atomic_json(root / "design.json", Design().model_dump())
        print(f"Created {root}. Add datasheets, sync a schematic, then index.")
        return
    if args.command == "doctor":
        try:
            cli = kicad_cli()
        except ValueError:
            cli = None
        emit({"python": sys.version.split()[0], "kicad_cli": cli,
              "ollama_executable": shutil.which("ollama"),
              "note": "No model inference, downloads, or API calls were performed."})
        return
    if args.command == "extract-parameters":
        from .evidence import EvidenceRequest
        from .parameter_extraction import propose_parameters
        project = load_project(root)
        result = propose_parameters(root, project, EvidenceRequest.model_validate_json(args.request.read_text()),
                                    lexical_only=args.lexical_only)
        atomic_json(args.output, result)
        emit(result)
        return 2  # Proposals require review; extraction is never acceptance.
    if args.command == "schema":
        from .board import BoardSpec
        from .evidence import EvidenceRequest, ParameterFact
        from .hierarchy import HierarchySpec
        from .native import Intent, SchematicSpec
        from .release import Inspection
        models = {"project": Project, "design": Design, "evidence-request": EvidenceRequest,
                  "parameter": ParameterFact, "schematic-spec": SchematicSpec, "intent": Intent, "hierarchy-spec": HierarchySpec, "board-spec": BoardSpec, "inspection": Inspection}
        emit(models[args.kind].model_json_schema())
        return
    if args.command == "generate-hierarchy":
        from .hierarchy import HierarchySpec, generate_hierarchy_checked
        from .native import Intent
        result = generate_hierarchy_checked(HierarchySpec.model_validate_json(args.spec.read_text()),
                                             Intent.model_validate_json(args.intent.read_text()), args.output.resolve())
        emit(result)
        return 0 if result["status"] == "pass" else 2
    if args.command in ("generate-schematic", "verify-native"):
        from .native import Intent, SchematicSpec, generate_checked, verify_board, verify_schematic
        intent = Intent.model_validate_json(args.intent.read_text())
        if args.command == "generate-schematic":
            result = generate_checked(SchematicSpec.model_validate_json(args.spec.read_text()), intent, args.output.resolve())
        else:
            result = verify_schematic(args.schematic.resolve(), intent, args.output.resolve())
            if args.board:
                result['board'] = verify_board(args.board.resolve(), intent, args.schematic.resolve(),
                                                args.output.resolve(), refill=args.refill)
        emit(result)
        return 2 if result['status'] != 'pass' or result.get('board', {}).get('status', 'pass') != 'pass' else 0
    if args.command == 'generate-board':
        from .board import BoardSpec, build_board
        from .native import Intent, verify_board
        schematic = args.schematic.resolve()
        intent = Intent.model_validate_json(args.intent.read_text())
        board = build_board(schematic, BoardSpec.model_validate_json(args.spec.read_text()), intent)
        result = verify_board(board, intent, schematic, schematic.parent / 'reports', refill=True)
        emit(result)
        return 0 if result['status'] == 'pass' else 2
    if args.command == "export-assembly":
        from .release import export_assembly
        emit(export_assembly(args.board.resolve(), args.output.resolve()))
        return 0
    if args.command == 'ocr-copy':
        from .advanced import ocr_copy
        emit(ocr_copy(args.source.resolve(), args.output.resolve()))
        return
    if args.command == 'source-diff':
        from .advanced import source_diff
        result = source_diff(args.previous.resolve(), args.current.resolve())
        atomic_json(args.output, result)
        emit(result)
        return
    if args.command == 'simulate':
        from .simulation import simulate
        from .simulation_models import ModelManifest
        models = ModelManifest.model_validate_json(args.models.read_text()) if args.models else None
        result = simulate(args.deck.resolve(), args.output.resolve(), models=models)
        emit(result)
        return 2 if result['status'] == 'fail' else 0
    if args.command == 'export-manufacturing':
        from .release import export_manufacturing
        emit(export_manufacturing(args.board.resolve(), args.output.resolve()))
        return
    if args.command == 'release':
        from .release import release
        schematic = args.schematic.resolve()
        emit(release(schematic.parent, schematic, args.intent.resolve(), args.visual_review.resolve(),
                     board=args.board.resolve() if args.board else None,
                     physical_path=args.physical_review.resolve() if args.physical_review else None))
        return
    if args.command == "gui":
        from .gui import launch
        launch(root)
        return
    project = load_project(root)
    if args.command == "calculate":
        from .evidence import calculate_packet
        result = calculate_packet(root, project, json.loads(args.evidence.read_text()),
                                  Design.model_validate_json(args.design.read_text()))
        atomic_json(args.output, result)
        emit(result)
        return 0 if result['status'] == 'within_stated_bounds' else 2
    if args.command in ("evidence", "verify-evidence", "impact", "accept-fact"):
        from .evidence import EvidenceRequest, evidence_packet, impact, review_fact, verify_packet
        if args.command == "evidence":
            result = evidence_packet(root, project, EvidenceRequest.model_validate_json(args.request.read_text()),
                                     lexical_only=args.lexical_only)
            atomic_json(args.output, result)
            emit(result)
            return 0 if result['complete'] else 2
        if args.command == "verify-evidence":
            result = verify_packet(root, project, json.loads(args.packet.read_text()))
            emit(result)
            return 0 if result['valid'] else 2
        if args.command == "impact":
            emit(impact(root, project))
            return
        emit(review_fact(root, project, args.id, args.actor, args.rationale))
        return
    if args.command == "add":
        source = args.file.expanduser().resolve()
        if source.suffix.lower() not in (".pdf", ".md", ".txt"):
            raise ValueError("Supported files: .pdf, .md, .txt")
        # Validate registration before copying and preserve old source snapshots.
        name = f"{digest(source)[:12]}-{source.name}"
        doc = Document(id=args.id, path=f"datasheets/{name}", title=args.title or source.stem,
                       parts=args.parts, revision=args.revision, kind=args.kind, source_url=args.source_url)
        destination = local_path(root, doc.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source != destination:
            shutil.copyfile(source, destination)
        project.documents = [d for d in project.documents if d.id != doc.id] + [doc]
        save_project(root, project)
        print(f"Registered {doc.id}: {doc.title}. Run index to apply this revision.")
    elif args.command == "remove":
        if not any(d.id == args.id for d in project.documents):
            raise ValueError(f"Unknown document {args.id}")
        project.documents = [d for d in project.documents if d.id != args.id]
        save_project(root, project)
        print("Removed registration; source file preserved. Run index.")
    elif args.command == "configure":
        values = project.settings.model_dump()
        for key in ("embedding_provider", "embedding_model", "chat_provider", "chat_model", "ollama_url",
                    "embedding_query_prefix", "embedding_document_prefix", "reranker_model", "restore_parent_context", "shared_extraction_cache", "ocr_mode", "ocr_language", "ocr_dpi"):
            if getattr(args, key) is not None:
                values[key] = getattr(args, key)
        project.settings = Settings.model_validate(values)
        save_project(root, project)
        emit(project.settings.model_dump())
        if "openai" in (project.settings.embedding_provider, project.settings.chat_provider):
            print("Cloud mode: embedding sends indexed document text; chat sends question, retrieved excerpts, topology and relevant checks.")
    elif args.command == "sync-kicad":
        source = args.schematic or (Path(project.schematic) if project.schematic else None)
        if not source:
            raise ValueError("Provide the path to your .kicad_sch file")
        export_schematic(root, project, source)
        print("Exported netlist and recorded schematic/child-sheet hashes. Run index.")
    elif args.command == "import-netlist":
        Graph.from_xml(args.file, project.component_parts)
        destination = root / "netlist.xml"
        if args.file.resolve() != destination.resolve():
            shutil.copyfile(args.file, destination)
        project.netlist, project.schematic, project.schematic_hashes = "netlist.xml", None, {}
        save_project(root, project)
        print("Imported netlist snapshot. Reimport after schematic changes; automatic schematic freshness is unavailable. Run index.")
    elif args.command == "map-part":
        graph = Graph.load(root, project)
        if args.ref not in graph.components:
            raise ValueError(f"Reference {args.ref} not found in the imported netlist")
        project.component_parts[args.ref] = args.part
        save_project(root, project)
        print("Part mapping updated. Run index.")
    elif args.command == "index":
        emit(index_project(root, project, force=args.force))
    elif args.command in ("ask", "search"):
        result = ask(root, project, args.question, args.refs, candidate_parts=args.parts,
                     evidence_only=args.evidence_only or args.command == "search",
                     lexical_only=args.lexical_only)
        if args.json:
            emit(result)
        else:
            print(render(result))
            print(f"Saved report: {result['report_path']}")
    elif args.command == "status":
        with Store(root) as store:
            try:
                store.require_fresh(root, project)
                freshness = "current"
            except (ValueError, OSError) as error:
                freshness = str(error)
            emit({"project": project.name, "freshness": freshness, "settings": project.settings.model_dump(),
                  "index": store.meta("stats"), "registered_documents": [d.model_dump() for d in project.documents]})
    elif args.command == "coverage":
        emit(Graph.load(root, project).coverage(project))
    elif args.command == "check":
        with Store(root) as store:
            store.require_fresh(root, project)
            reports = evaluate(load_design(root, project), Graph.load(root, project), project, store)
        result = {"checks": reports, "note": "No configured checks means nothing was assessed. Within stated bounds does not approve the design."}
        atomic_json(root / "reports" / "checks.json", result)
        emit(result)
        if any(c["status"] != "within_stated_bounds" for c in reports):
            return 2
    elif args.command == "page":
        with Store(root) as store:
            store.require_fresh(root, project)
            print(store.page(args.document, args.number))
        if args.render:
            import pdfplumber
            doc = next(d for d in project.documents if d.id == args.document)
            if Path(doc.path).suffix.lower() != ".pdf":
                raise ValueError("Rendering requires a PDF source")
            args.render.parent.mkdir(parents=True, exist_ok=True)
            with pdfplumber.open(local_path(root, doc.path)) as pdf:
                pdf.pages[args.number - 1].to_image(resolution=150).save(str(args.render))
            print(f"Rendered {args.render.resolve()}")
    elif args.command == "path":
        emit({"path": Graph.load(root, project).path(args.start, args.end),
              "note": "Topological path only; shared ground/power does not establish functional causality."})
    return 0


def main():
    try:
        code = run(parser().parse_args())
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    raise SystemExit(code or 0)


if __name__ == "__main__":
    main()
