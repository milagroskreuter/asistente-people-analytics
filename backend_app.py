# -*- coding: utf-8 -*-
"""
Backend del Asistente de People Analytics (sirve la web + API en tiempo real).

Flujo: la web manda una pregunta -> un LLM (Groq) la traduce a SQL -> corre en BigQuery
-> el LLM redacta la respuesta -> vuelve a la web. Las credenciales viven SOLO aca.

Sirve TODO desde una sola URL: la pagina en "/" y la API en "/ask".
Listo para correr local (python backend_app.py) o desplegado (gunicorn).

Variables de entorno:
    GROQ_API_KEY        (obligatoria)  key de Groq
    GCP_PROJECT         (obligatoria)  id del proyecto de Google Cloud
    BQ_DATASET          people_analytics
    GROQ_MODEL          llama-3.3-70b-versatile   (opcional)
    ACCESS_TOKEN        token simple para proteger la API (opcional pero recomendado)
    PORT                puerto (lo setea el host; local default 8000)
    Credenciales de BigQuery, una de estas dos:
      GOOGLE_CREDENTIALS_JSON        contenido del .json de la service account (para la nube)
      GOOGLE_APPLICATION_CREDENTIALS ruta al .json (para correr local)

Requisitos:
    pip install flask flask-cors google-cloud-bigquery requests gunicorn
"""

import os, re, json, requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "mi-proyecto-gcp")
DATASET = os.environ.get("BQ_DATASET", "people_analytics")
GROQ_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))

# --- BigQuery: credenciales desde env JSON (nube) o desde archivo local ---
_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if _creds_json:
    from google.oauth2 import service_account
    _creds = service_account.Credentials.from_service_account_info(json.loads(_creds_json))
    bq = bigquery.Client(project=PROJECT, credentials=_creds)
else:
    bq = bigquery.Client(project=PROJECT)

def llm(prompt):
    r = requests.post(GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
        json={"model": GROQ_MODEL, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=40)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

SCHEMA = f"""Base en BigQuery: `{PROJECT}.{DATASET}` (nombres de columna en snake_case)

Tabla ees_clean (empleados), columnas:
  employee, department, location_country, region (US/LatAm/Other),
  employment_status, termination_type, type_of_hire (Payroll/Contractor/Outsourced),
  annual_compensation_usd (numero), job_title, tenure_years, is_active, is_involuntary

Tabla ta_clean (busquedas / recruiting), columnas:
  recruiter, priority (High/Medium/Low), time_to_fill (dias), status, department,
  open_date, start_date

Reglas de negocio:
  - Headcount activo = filas de ees_clean con employment_status <> 'Terminated'.
  - Turnover involuntario = filas con termination_type = 'Termination (Involuntary)'.
    Tasa = involuntarias / total del grupo. Para rankings por grupo, filtra grupos con 20+ personas.
  - Time-to-fill: usa la MEDIANA con APPROX_QUANTILES(time_to_fill, 2)[OFFSET(1)].
  - Vacantes abiertas = filas de ta_clean con status = 'Active'.
"""

def generar_sql(pregunta, rol):
    guard = ""
    if rol == "manager":
        guard = ("RESTRICCION: el rol Manager NO puede acceder a compensacion. "
                 "Si la pregunta pide annual_compensation_usd o sueldos, responde unicamente con la palabra RESTRICTED.")
    prompt = f"""Sos un generador de SQL para BigQuery (Standard SQL).
{SCHEMA}
{guard}
Reglas estrictas para no equivocarte:
- Escribi UNA sola consulta SELECT que responda: "{pregunta}".
- NO uses JOIN salvo que sea imprescindible; preferi consultar una sola tabla.
- Vacantes abiertas = COUNT(*) de ta_clean con status = 'Active'. NUNCA cuentes recruiters ni uses DISTINCT para contar vacantes.
- Cuida la granularidad: no dupliques filas. Si dudas, usa la consulta mas simple y directa.
- Cada pregunta es independiente: no asumas contexto de preguntas anteriores. Si es ambigua, tomá la interpretacion mas literal.
- Para la mediana usa APPROX_QUANTILES(time_to_fill, 2)[OFFSET(1)].
Usa nombres de tabla completos con backticks, por ej. `{PROJECT}.{DATASET}.ees_clean`. Columnas en snake_case.
Devolve SOLO el SQL, sin explicacion y sin marcas de codigo."""
    sql = llm(prompt).strip()
    # Saco solo las marcas de bloque de codigo (```sql ... ```), sin tocar los backticks
    # de los identificadores (tablas/columnas), que SI deben quedar.
    if sql.startswith("```"):
        sql = re.sub(r"^```[a-zA-Z]*\s*", "", sql)
        sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()

def es_seguro(sql):
    s = " " + sql.lower().strip() + " "
    if not s.strip().startswith("select"):
        return False
    return not any(f" {w} " in s for w in
                   ["insert", "update", "delete", "drop", "merge", "create", "alter", "truncate", "grant"])

app = Flask(__name__)
CORS(app)

@app.route("/")
def home():
    html = open(os.path.join(HERE, "Asistente_Consultas_RRHH.html"), encoding="utf-8").read()
    # inyecto la config para que la web apunte a esta misma URL y mande el token, y arranque en vivo
    cfg = (f"<script>window.APP_BACKEND='/ask';"
           f"window.APP_TOKEN={json.dumps(ACCESS_TOKEN)};"
           f"window.APP_DEFAULT_ENGINE='live';</script>")
    html = html.replace("</head>", cfg + "</head>", 1)
    return Response(html, mimetype="text/html")

@app.route("/ask", methods=["POST"])
def ask():
    if ACCESS_TOKEN and request.headers.get("X-Access-Token", "") != ACCESS_TOKEN:
        return jsonify({"answer": "Acceso no autorizado."}), 401
    data = request.get_json(force=True)
    pregunta = (data.get("question") or "").strip()
    rol = data.get("role", "director")
    if not pregunta:
        return jsonify({"answer": "Escribi una pregunta."})
    sql = ""
    try:
        sql = generar_sql(pregunta, rol)
        # Control de acceso determinístico (no se confía en el LLM): si el rol es Manager
        # y la consulta toca compensación, se bloquea acá sí o sí.
        if rol == "manager" and any(t in sql.lower() for t in ["annual_compensation_usd", "compensation", "compensa"]):
            return jsonify({"answer": "Compensacion restringida: el rol Manager no tiene acceso a datos de compensacion.", "sql": ""})
        if "restricted" in sql.lower():
            return jsonify({"answer": "Compensacion restringida: el rol Manager no tiene acceso a datos de compensacion.", "sql": ""})
        if not es_seguro(sql):
            return jsonify({"answer": "No pude generar una consulta segura para esa pregunta.", "sql": sql})
        rows = [dict(r) for r in bq.query(sql).result()]
        resumen = llm(
            f'Pregunta del usuario: "{pregunta}".\nResultado (JSON): {json.dumps(rows[:50], default=str)}.\n'
            f"Redacta una respuesta breve y clara en espaniol, mencionando los numeros clave. No muestres el SQL."
        )
        return jsonify({"answer": resumen, "sql": sql, "rows": rows[:50]})
    except Exception as e:
        return jsonify({"answer": f"Hubo un error ejecutando la consulta: {e}", "sql": sql})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Backend escuchando en http://localhost:{port}  (LLM: Groq / {GROQ_MODEL})")
    app.run(host="0.0.0.0", port=port, debug=False)
