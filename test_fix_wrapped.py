"""Tests de fix_wrapped_code, foco en heredocs de código rotos por el wrap.

Correr con el venv que tiene las deps:  ./env/bin/python test_fix_wrapped.py
"""
import re
import popup

fix = popup.fix_wrapped_code
fails = []


def check(name, cond):
    print(('  ok  ' if cond else 'FAIL  ') + name)
    if not cond:
        fails.append(name)


def heredoc_body(text, marker):
    """Extrae el cuerpo del heredoc <<marker ... marker para compilarlo."""
    m = re.search(r"<<[-'\"]*%s['\"]*\n(.*?)\n%s" % (marker, marker),
                  text, re.DOTALL)
    return m.group(1) if m else None


def charwrap(s, w):
    """Simula el wrap *duro* del terminal: corta cada línea lógica en la
    columna w sin respetar tokens (así nace `LoanSta`+`tus`)."""
    res = []
    for logical in s.split('\n'):
        if logical == '':
            res.append('')
            continue
        while len(logical) > w:
            res.append(logical[:w])
            logical = logical[w:]
        res.append(logical)
    return '\n'.join(res)


# --- Caso real del usuario: heredoc python con dos roturas fatales ---
# 1) string de una línea partido por el wrap (SELECT ... AND ... excluded...)
# 2) identificador partido a la mitad (LoanSta\ntus.PAID_OFF)
# El cuerpo es código real, char-wrappeado a 72 columnas como un terminal.
SCRIPT = (
    "import sys; sys.path.insert(0, '/app')\n"
    "import re\n"
    "from sqlalchemy import select, text\n"
    "TERM = {str(s) for s in [LoanStatus.APPROVED, LoanStatus.APPROVED_CREDIWEB, "
    "LoanStatus.DISBURSED, LoanStatus.PAID_OFF, LoanStatus.DEFAULTED, "
    "LoanStatus.REJECTED, LoanStatus.CANCELLED]}\n"
    "norm = lambda c: re.sub(r'\\D', '', c or '')\n"
    "def go():\n"
    "    q = text(\"SELECT cuit_normalized, name FROM convenio_employers WHERE "
    "cuit_normalized IS NOT NULL AND active = true AND excluded_by_admin = false\")\n"
    "    by_st = {}\n"
    "    print(sum(n for s, n in by_st.items() if s not in TERM), q)\n"
    "go()\n"
)
USER = ("docker exec -i df-worker-prod python - <<'PYEOF'\n"
        + charwrap(SCRIPT, 72) + "\nPYEOF")

out = fix(USER)
body = heredoc_body(out, 'PYEOF')
check('heredoc body extraído', body is not None)
check('identificadores reunidos (LoanStatus.PAID_OFF intacto)',
      'LoanStatus.PAID_OFF' in (body or ''))
check('SELECT reunido en una línea ejecutable',
      'AND active = true AND excluded_by_admin = false' in (body or ''))
ok = False
if body is not None:
    try:
        compile(body, '<heredoc>', 'exec')
        ok = True
    except SyntaxError as e:
        print('    SyntaxError:', e)
check('cuerpo del heredoc compila como Python', ok)


# --- Regresión: código normal NO se corrompe ---
imports = "import os\nimport sys\nimport json\n" + ("x = " + "a" * 80 + "\n")
# (incluye una línea larga para que maxw>=50 y la pasada se active)
r = fix(imports)
check('import os/sys/json intactos',
      'import os\nimport sys\nimport json' in r)

# Asignaciones largas al tope no se pegan entre sí
asg = ("configuration_value_for_the_subsystem = computed_result_here_xx\n"
       "fallback_value = None\n")
r2 = fix(asg)
check('asignaciones largas separadas no se pegan',
      'computed_result_here_xx\nfallback_value' in r2)

# Heredoc de TEXTO (cat) se preserva verbatim (incluso con comilla suelta,
# que es justo el caso por el que existe el modo verbatim).
cat_body = ("it's a long literal line that should stay exactly as written ok\n"
            "second long literal line that also must remain fully untouched x")
cat = "cat <<EOF\n" + cat_body + "\nEOF\n"
r3 = fix(cat)
check('heredoc de texto (cat) intacto byte a byte', cat_body in r3)

# Dos líneas de código normal del MISMO largo (>=50) no se pegan entre sí
samew = ("alpha_variable_name_one = compute_something(a, b, c, d, ee)\n"
         "betaa_variable_name_two = compute_something(a, b, c, d, ee)\n")
assert len(samew.split('\n')[0]) == len(samew.split('\n')[1])  # mismo ancho
r5 = fix(samew)
check('dos líneas de igual ancho no se concatenan',
      'ee)\nbetaa_variable_name_two' in r5)

# Wrap dentro de string a nivel top (sin heredoc) sigue andando
top = 'q = "SELECT a, b FROM t WHERE x = 1 AND\ny = 2 ORDER BY a LIMIT 100000"\n'
r4 = fix(top)
check('string top-level desenrollado con espacio',
      'AND y = 2' in r4 and '\n' not in r4.split('"')[1])

print()
if fails:
    print('FALLARON %d test(s): %s' % (len(fails), ', '.join(fails)))
    raise SystemExit(1)
print('todos los tests OK')
