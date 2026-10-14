"""Small native desktop interface; model work runs off the Tk event loop."""
from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from .checks import evaluate, load_design
from .config import Document, Project, Settings, atomic_json, digest, load_project, local_path, save_project
from .kicad import Graph, export_schematic
from .rag import ask, render
from .store import Store, index_project


def launch(initial: Path):
    window = tk.Tk()
    window.title("KiCad Datasheet Assistant")
    window.geometry("1120x820")
    window.minsize(820, 640)
    style = ttk.Style(window)
    style.configure("Title.TLabel", font=("Helvetica", 23, "bold"))
    style.configure("TButton", padding=(10, 6))
    main = ttk.Frame(window, padding=22)
    main.pack(fill="both", expand=True)
    ttk.Label(main, text="KiCad Datasheet Assistant", style="Title.TLabel").pack(anchor="w")
    ttk.Label(main, text="Project evidence • component connections • conditional engineering checks").pack(anchor="w", pady=(4, 14))
    folder = tk.StringVar(value=str(initial))
    status = tk.StringVar(value="Choose a project. The bundled demo works without a model service.")
    pathbar = ttk.Frame(main)
    pathbar.pack(fill="x")
    path_entry = ttk.Entry(pathbar, textvariable=folder)
    path_entry.pack(side="left", fill="x", expand=True)
    buttons = []
    results = queue.Queue()
    cancellation = threading.Event()

    def show(text):
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("1.0", text)
        output.configure(state="disabled")

    def run_job(label, operation):
        cancellation.clear()
        progress.start(12)
        cancel_button.configure(state='normal' if label.startswith('Extracting') else 'disabled')
        for button in buttons:
            button.configure(state="disabled")
        path_entry.configure(state="disabled")
        status.set(label)

        def worker():
            try:
                value = operation()
                results.put((True, value if isinstance(value, str) else json.dumps(value, indent=2)))
            except Exception as error:
                results.put((False, str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def poll():
        try:
            ok, text = results.get_nowait()
            progress.stop()
            cancel_button.configure(state='disabled')
            show(text)
            status.set("Finished. Review sources and conditions below." if ok else "Could not complete this operation; see details below.")
            for button in buttons:
                button.configure(state="normal")
            path_entry.configure(state="normal")
        except queue.Empty:
            pass
        window.after(100, poll)

    def button(parent, label, command):
        obj = ttk.Button(parent, text=label, command=command)
        obj.pack(side="left", padx=(0, 6), pady=5)
        buttons.append(obj)

    def choose():
        value = filedialog.askdirectory(title="Choose a folder with project.json")
        if value:
            folder.set(value)

    button(pathbar, "Open project", choose)

    def create():
        parent = filedialog.askdirectory(title="Choose parent folder for a new project")
        if not parent:
            return
        name = simpledialog.askstring("New project", "Project folder name (letters, numbers, hyphens):")
        if not name:
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            messagebox.showerror("Invalid name", "Use letters, numbers, underscores or hyphens.")
            return
        root = Path(parent) / name
        if (root / "project.json").exists():
            messagebox.showerror("Already exists", "Open this project instead.")
            return
        save_project(root, Project(name=name))
        (root / "datasheets").mkdir(exist_ok=True)
        folder.set(str(root))
        show("Project created. Add documents and sync your saved schematic, then build the index.")

    button(pathbar, "New project", create)
    toolbar = ttk.Frame(main)
    toolbar.pack(fill="x", pady=(8, 6))

    def project_job(label, action):
        root = Path(folder.get()).expanduser().resolve()
        run_job(label, lambda: action(root, load_project(root)))

    def add_document():
        file = filedialog.askopenfilename(filetypes=[("Datasheets and notes", "*.pdf *.md *.txt")])
        if not file:
            return
        identifier = simpledialog.askstring("Document ID", "Stable document ID, e.g. regulator-revb:")
        if not identifier:
            return
        parts = simpledialog.askstring("Exact parts", "Manufacturer part numbers, comma separated:")
        if parts is None:
            return
        revision = simpledialog.askstring("Revision", "Document revision:", initialvalue="unspecified")
        if revision is None:
            return

        def add(root, project):
            source = Path(file)
            doc = Document(id=identifier, title=source.stem, parts=[p.strip() for p in parts.split(",") if p.strip()],
                           revision=revision, path=f"datasheets/{digest(source)[:12]}-{source.name}")
            target = root / doc.path
            target.parent.mkdir(exist_ok=True)
            if source.resolve() != target.resolve():
                shutil.copyfile(source, target)
            project.documents = [d for d in project.documents if d.id != doc.id] + [doc]
            save_project(root, project)
            return "Document registered. Build the index to apply the change."
        project_job("Registering document…", add)

    def sync():
        path = filedialog.askopenfilename(filetypes=[("KiCad schematic", "*.kicad_sch")])
        if path:
            project_job("Exporting KiCad connections…", lambda r, p: (export_schematic(r, p, Path(path)) or "Schematic exported. Build the index."))

    def configure():
        root = Path(folder.get()).expanduser().resolve()
        try:
            project = load_project(root)
        except (ValueError, OSError) as error:
            messagebox.showerror("Project", str(error))
            return
        dialog = tk.Toplevel(window)
        dialog.title("Model settings")
        dialog.transient(window)
        dialog.grab_set()
        values = {}
        for i, key in enumerate(("embedding_provider", "embedding_model", "chat_provider", "chat_model")):
            ttk.Label(dialog, text=key.replace("_", " ").title()).grid(row=i, column=0, padx=12, pady=8, sticky="w")
            values[key] = tk.StringVar(value=getattr(project.settings, key))
            if key.endswith("provider"):
                widget = ttk.Combobox(dialog, textvariable=values[key], values=["none", "ollama", "openai"], state="readonly")
            else:
                widget = ttk.Entry(dialog, textvariable=values[key], width=32)
            widget.grid(row=i, column=1, padx=12, pady=8)
        ttk.Label(dialog, text="Ollama runs locally. OpenAI sends text to the cloud and requires\nOPENAI_API_KEY in the launch environment. Model names must exist.\nEmbedding changes require rebuilding the index.").grid(row=4, columnspan=2, padx=12, pady=10)

        def save():
            try:
                project.settings = Settings.model_validate({**project.settings.model_dump(), **{k: v.get() for k, v in values.items()}})
                save_project(root, project)
                dialog.destroy()
                status.set("Settings saved. Rebuild the index if embedding settings changed.")
            except ValueError as error:
                messagebox.showerror("Invalid settings", str(error), parent=dialog)
        ttk.Button(dialog, text="Save settings", command=save).grid(row=5, columnspan=2, pady=12)

    button(toolbar, "Add datasheet", add_document)
    button(toolbar, "Sync KiCad", sync)
    button(toolbar, "Build index", lambda: project_job("Extracting and indexing documents…", lambda r, p: index_project(r, p, cancel=cancellation)))
    button(toolbar, "Coverage", lambda: project_job("Reading components…", lambda r, p: Graph.load(r, p).coverage(p)))
    button(toolbar, "Model settings", configure)
    ttk.Label(main, text="Ask a question. Include component references such as U1 and U2 for circuit context.").pack(anchor="w", pady=(8, 4))
    question = ScrolledText(main, height=3, font=("Helvetica", 13), wrap="word")
    question.pack(fill="x")
    question.insert("1.0", "Can U1 drive U2 reliably? Explain the logic-level margins and any missing checks.")
    actions = ttk.Frame(main)
    actions.pack(fill="x")

    def query(evidence):
        text = question.get("1.0", "end").strip()
        project_job("Retrieving evidence…" if evidence else "Retrieving evidence and generating cited answer…",
                    lambda r, p: render(ask(r, p, text, evidence_only=evidence)))

    def checks(root, project):
        with Store(root) as store:
            store.require_fresh(root, project)
            return evaluate(load_design(root, project), Graph.load(root, project), project, store)

    button(actions, "Find evidence", lambda: query(True))
    button(actions, "Generate answer", lambda: query(False))
    button(actions, "Run design checks", lambda: project_job("Checking reviewed values…", checks))
    def parameters():
        from .evidence import ParameterFact, facts, impact, review_fact
        root = Path(folder.get()).expanduser().resolve()
        try:
            project = load_project(root)
            records = facts(root)
            issues = {r['fact']: r['issues'] for r in impact(root, project)}
        except (ValueError, OSError) as error:
            messagebox.showerror('Parameter review', str(error))
            return
        dialog = tk.Toplevel(window)
        dialog.title('Parameter facts and original sources')
        dialog.geometry('1040x760')
        listing = ttk.Treeview(dialog, columns=('part', 'parameter', 'value', 'state'), show='headings', height=7)
        for col in ('part', 'parameter', 'value', 'state'):
            listing.heading(col, text=col.title())
        listing.pack(fill='x', padx=10, pady=10)
        for fact in records:
            listing.insert('', 'end', iid=fact.id, values=(fact.candidate.part, fact.parameter,
                f'{fact.value} {fact.unit}', ', '.join(issues[fact.id]) or fact.state))
        details = ScrolledText(dialog, height=13, wrap='word')
        details.pack(fill='x', padx=10)
        controls = ttk.Frame(dialog)
        controls.pack(fill='x')
        canvas = tk.Canvas(dialog, background='white')
        scrollbar = ttk.Scrollbar(dialog, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(fill='both', expand=True)
        viewed = set()

        def selected():
            return next((f for f in records if listing.selection() and f.id == listing.selection()[0]), None)

        def source(_=None):
            fact = selected()
            if not fact:
                return
            details.delete('1.0', 'end')
            details.insert('1.0', json.dumps(fact.model_dump(), indent=2))
            try:
                doc = next(d for d in project.documents if d.id == fact.source.document)
                path = local_path(root, doc.path)
                canvas.delete('all')
                if path.suffix.lower() == '.pdf':
                    import pdfplumber
                    from PIL import ImageTk
                    with pdfplumber.open(path) as pdf:
                        page = pdf.pages[fact.source.page - 1].to_image(resolution=110)
                        if fact.source.bbox:
                            page.draw_rect(fact.source.bbox, fill=None, stroke='red', stroke_width=2)
                        canvas.source_image = ImageTk.PhotoImage(page.annotated)
                    canvas.create_image(0, 0, anchor='nw', image=canvas.source_image)
                else:
                    canvas.create_text(10, 10, anchor='nw', text=path.read_text().split('\f')[fact.source.page - 1], width=950)
                canvas.configure(scrollregion=canvas.bbox('all'))
                viewed.add(fact.id)
            except (ValueError, OSError, IndexError, StopIteration) as error:
                messagebox.showerror('Original source', str(error), parent=dialog)

        def accept():
            fact = selected()
            if fact is None or fact.id not in viewed:
                messagebox.showerror('Review', 'Select a fact and inspect its original page first.', parent=dialog)
                return
            rationale = simpledialog.askstring('Record completed review', 'Describe the checked parameter, min/typ/max column, units, package and applicable conditions:', parent=dialog)
            if not rationale:
                return
            try:
                import getpass
                review_fact(root, project, fact.id, getpass.getuser(), rationale)
                dialog.destroy()
                parameters()
            except ValueError as error:
                messagebox.showerror('Review not accepted', str(error), parent=dialog)

        def import_facts():
            file = filedialog.askopenfilename(parent=dialog, filetypes=[('Parameter JSON', '*.json')])
            if not file:
                return
            try:
                data = json.loads(Path(file).read_text())
                if isinstance(data, dict) and "results" in data:
                    data = [p["fact"] for result in data["results"] for p in result["proposals"]]
                new = [ParameterFact.model_validate(f) for f in data]
                for f in new:
                    f.state, f.review = 'extracted', None
                merged = {f.id: f for f in records}
                if set(merged).intersection(f.id for f in new):
                    raise ValueError('Imported IDs already exist; use distinct IDs to preserve review history')
                merged.update({f.id: f for f in new})
                atomic_json(root / 'parameters.json', [f.model_dump() for f in merged.values()])
                dialog.destroy()
                parameters()
            except (ValueError, OSError) as error:
                messagebox.showerror('Import facts', str(error), parent=dialog)
        def edit_fact():
            fact = selected()
            if not fact:
                return
            editor = tk.Toplevel(dialog)
            editor.title("Correct extracted fields from the original source")
            editor.geometry("850x650")
            ttk.Label(editor, text="Correct parameter, category, bound, package and conditions. Saving clears prior acceptance.").pack()
            text = ScrolledText(editor, wrap="word")
            text.pack(fill="both", expand=True)
            text.insert("1.0", json.dumps(fact.model_dump(), indent=2))
            def save():
                try:
                    data = json.loads(text.get("1.0", "end"))
                    data["state"], data["review"] = "needs_review", None
                    revised = ParameterFact.model_validate(data)
                    if revised.id != fact.id:
                        raise ValueError("Preserve this record ID; import a new record for a different source")
                    atomic_json(root / "parameters.json", [revised.model_dump() if f.id == fact.id else f.model_dump() for f in records])
                    editor.destroy()
                    dialog.destroy()
                    parameters()
                except (ValueError, OSError) as error:
                    messagebox.showerror("Invalid fact", str(error), parent=editor)
            ttk.Button(editor, text="Save corrections for review", command=save).pack(pady=8)
        listing.bind('<<TreeviewSelect>>', source)
        ttk.Button(controls, text='Import extracted facts', command=import_facts).pack(side='left', padx=8, pady=8)
        ttk.Button(controls, text='Edit fields', command=edit_fact).pack(side='left', padx=8)
        ttk.Button(controls, text='Accept after original-page review', command=accept).pack(side='left', padx=8)
    button(actions, 'Parameter review', parameters)
    button(actions, 'Source change impact', lambda: project_job('Checking source dependencies…', lambda r, p: __import__('kicad_rag.evidence', fromlist=['impact']).impact(r, p)))
    progress = ttk.Progressbar(main, mode='indeterminate')
    progress.pack(fill='x', pady=(4, 0))
    cancel_button = ttk.Button(main, text='Cancel indexing (after current extraction/model request)', state='disabled', command=cancellation.set)
    cancel_button.pack(anchor='e')
    ttk.Label(main, textvariable=status, wraplength=1020).pack(anchor="w", pady=(6, 10))
    output = ScrolledText(main, font=("Menlo", 11), wrap="word", state="disabled")
    output.pack(fill="both", expand=True)
    show("Start with Find evidence for an offline evidence packet.\n\nGenerated answers require a configured model. Every project has a separate index.\n\nNumerical checks use reviewed values in design.json; the demo illustrates the format.\nCitation checks confirm quoted text exists, but engineering conclusions still need review.")
    poll()
    window.mainloop()
