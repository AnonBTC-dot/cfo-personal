#!/usr/bin/env python3
"""
Comprueba que el JavaScript embebido en app.py es válido.

Motivo: el HTML vive dentro de una plantilla de Python (DASH = \"\"\"...\"\"\"),
así que Python interpreta las secuencias de escape ANTES de servir la página.
Un `\n` dentro de una cadena JS se convierte en un salto de línea real y rompe
el script entero: la app se queda cargando sin dar ningún error visible.

Uso:  python3 check_js.py     (necesita node para validar la sintaxis)
"""
import importlib.util, os, re, subprocess, sys, tempfile

os.environ.setdefault("CFO_DB_PATH", tempfile.mktemp(suffix=".db"))
spec = importlib.util.spec_from_file_location("cfo", "app.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fallos = 0

# 1) Escapes de un solo backslash que Python se comería
raw = open("app.py", encoding="utf-8").read()
i = raw.index('DASH = """'); j = raw.index('"""', i + 10)
malos = re.findall(r'(?<!\\)\\([ntrbfv0xuU])', raw[i:j])
if malos:
    print(f"✗ Escapes sin doblar dentro de DASH: {sorted(set(malos))}")
    print("  Escríbelos como \\\\n para que lleguen al navegador.")
    fallos += 1

# 2) Sintaxis de cada bloque <script>
for n, js in enumerate(re.findall(r"<script>(.*?)</script>", mod.DASH, re.S), 1):
    if len(js.strip()) < 50:
        continue
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js); ruta = f.name
    r = subprocess.run(["node", "--check", ruta], capture_output=True, text=True)
    os.unlink(ruta)
    if r.returncode:
        print(f"✗ Error de sintaxis en el bloque <script> #{n}:\n{r.stderr}")
        fallos += 1

print("✓ JavaScript válido" if not fallos else f"\n{fallos} problema(s)")
sys.exit(1 if fallos else 0)
