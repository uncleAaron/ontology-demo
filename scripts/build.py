"""Validate source and produce a dependency-free source release archive."""
import compileall
import subprocess
import tarfile
from pathlib import Path
root=Path(__file__).resolve().parent.parent
assert compileall.compile_dir(root/'app',quiet=1)
subprocess.run(['node','--check',str(root/'static/app.js')],check=True)
for name in ['static/index.html','static/style.css','README.md','requirements.txt']:
    assert (root/name).is_file(),name
out=root/'dist';out.mkdir(exist_ok=True)
with tarfile.open(out/'ontology-demo.tar.gz','w:gz') as archive:
    for folder in ['app','static','tests','scripts','docs']:
        for p in sorted((root/folder).rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts:
                archive.add(p,arcname='ontology-demo/'+str(p.relative_to(root)))
    for name in ['README.md','requirements.txt','requirements-dev.txt','requirements.lock','Dockerfile','compose.yaml','Makefile','.dockerignore','.gitignore','.env.example']:
        archive.add(root/name,arcname='ontology-demo/'+name)
print(out/'ontology-demo.tar.gz')
