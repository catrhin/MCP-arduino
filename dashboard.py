import base64
import io
import math
import os
import struct
import wave

import altair as alt
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Monitor de Biomateriales",
    page_icon="🧪",
    layout="wide",
)

# ===================== CONFIGURACIÓN THINGSPEAK =====================
CHANNEL_ID = "3518209"        # Ej: "1234567"
READ_API_KEY = "FTYO10HMF6994OT8"      # Solo si el canal es privado; si es público, déjalo ""

# Qué campo de ThingSpeak guarda cada medición (revisa el orden en tu canal)
FIELD_TEMP = "field1"  # Temperatura del aire (DHT11)
FIELD_HUM = "field2"   # Humedad del aire (DHT11)
FIELD_SUELO = None     # Ej: "field3" si subes la humedad del sensor enterrable

ZONA_HORARIA = "America/Santiago"
REFRESCO = "20s"       # ThingSpeak gratis acepta datos cada 15 s como mínimo
MINUTOS_SIN_DATOS = 10 # Si el último dato es más viejo, se avisa que no llegan datos
# ====================================================================


def _config(nombre, defecto):
    """Lee de st.secrets o variables de entorno; si no hay, usa el valor del código."""
    try:
        return st.secrets[nombre]
    except Exception:
        return os.getenv(nombre, defecto)


CHANNEL_ID = str(_config("THINGSPEAK_CHANNEL_ID", CHANNEL_ID)).strip()
READ_API_KEY = str(_config("THINGSPEAK_READ_API_KEY", READ_API_KEY)).strip()

# ============================ ESTILO ================================
st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem; max-width: 1250px;}
    .hero {
        background: linear-gradient(120deg, #0f766e 0%, #15803d 55%, #65a30d 100%);
        padding: 1.6rem 2rem; border-radius: 18px; color: white;
        margin-bottom: 1.2rem; box-shadow: 0 6px 18px rgba(21,128,61,.25);
    }
    .hero h1 {margin: 0; font-size: 2rem; color: white;}
    .hero p {margin: .3rem 0 0 0; opacity: .92;}
    div[data-testid="stMetric"] {
        background: rgba(120,160,130,.10);
        border: 1px solid rgba(120,160,130,.35);
        padding: 1rem 1.2rem; border-radius: 14px;
    }
    div[data-testid="stMetricValue"] {font-size: 2.1rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================ DATOS =================================
MUESTRAS = [
    {
        "Muestra": "A01",
        "Material base": "Papel reciclado",
        "Adhesivo": "Almidón",
        "Límite humedad (% RH)": 65,
        "Adhesión": "Media",
        "Biodegradación": "Alta",
        "Próxima acción": "Repetir ensayo en humedad",
    },
    {
        "Muestra": "B01",
        "Material base": "Celulosa",
        "Adhesivo": "Biobasado",
        "Límite humedad (% RH)": 75,
        "Adhesión": "Alta",
        "Biodegradación": "Media",
        "Próxima acción": "Priorizar prueba de adhesión",
    },
]


@st.cache_resource
def sonido_alarma_b64():
    """Genera una alarma corta (cinco pitidos) como WAV; no depende de internet."""
    tasa = 22050
    muestras = []
    for freq in (880, 660, 880, 660, 880):
        n = int(tasa * 0.18)
        for k in range(n):
            suavizado = min(1.0, k / 200, (n - k) / 200)
            muestras.append(int(32767 * 0.5 * suavizado * math.sin(2 * math.pi * freq * k / tasa)))
        muestras.extend([0] * int(tasa * 0.05))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(tasa)
        w.writeframes(struct.pack(f"<{len(muestras)}h", *muestras))
    return base64.b64encode(buf.getvalue()).decode()


@st.cache_data(ttl=15, show_spinner=False)
def descargar_feeds(channel_id: str, api_key: str, resultados: int):
    """Pide a ThingSpeak las últimas N lecturas del canal."""
    url = f"https://api.thingspeak.com/channels/{channel_id}/feeds.json"
    params = {"results": resultados}
    if api_key:
        params["api_key"] = api_key

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("Respuesta inesperada: revisa Channel ID y Read API Key.")
    return data.get("feeds", [])


def leer_datos(resultados: int):
    """Devuelve (DataFrame limpio, mensaje_de_error)."""
    if not CHANNEL_ID:
        return None, "Falta el Channel ID. Complétalo en CHANNEL_ID, al inicio del archivo."

    try:
        feeds = descargar_feeds(CHANNEL_ID, READ_API_KEY, resultados)
    except requests.HTTPError as e:
        codigo = e.response.status_code if e.response is not None else "?"
        if codigo in (400, 404):
            return None, (
                f"ThingSpeak respondió {codigo}. Revisa que el Channel ID sea correcto "
                "y, si el canal es privado, que READ_API_KEY esté completa."
            )
        return None, f"Error HTTP {codigo} al consultar ThingSpeak."
    except requests.RequestException as e:
        return None, f"No hay conexión con ThingSpeak ({type(e).__name__})."
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    if not feeds:
        return None, "El canal existe, pero todavía no tiene registros. ¿El ESP32 está enviando datos?"

    df = pd.DataFrame(feeds)

    columnas = {"temp": FIELD_TEMP, "hum": FIELD_HUM}
    if FIELD_SUELO:
        columnas["suelo"] = FIELD_SUELO

    for nombre, campo in columnas.items():
        if campo in df.columns:
            df[nombre] = pd.to_numeric(df[campo], errors="coerce")
        else:
            df[nombre] = float("nan")

    # ThingSpeak entrega la hora en UTC: se convierte a hora local
    df["fecha"] = (
        pd.to_datetime(df["created_at"], utc=True)
        .dt.tz_convert(ZONA_HORARIA)
        .dt.tz_localize(None)
    )

    df = df.dropna(subset=["temp", "hum"]).sort_values("fecha")
    if df.empty:
        return None, (
            "Hay registros, pero sin valores en "
            f"{FIELD_TEMP} / {FIELD_HUM}. Revisa qué campo usa tu ESP32 para cada medición."
        )

    cols = ["fecha", "temp", "hum"] + (["suelo"] if "suelo" in df.columns else [])
    return df[cols].reset_index(drop=True), None


RANGO_EJE = 20     # variación visible en el eje vertical (en % RH o en °C)
MAX_PUNTOS = 80    # máximo de puntos dibujados por gráfico (más = más detalle)


def adelgazar(df, max_puntos=MAX_PUNTOS):
    """Deja menos puntos para que el gráfico se lea; siempre conserva el último."""
    if len(df) <= max_puntos:
        return df
    paso = math.ceil(len(df) / max_puntos)
    return df.iloc[::-1].iloc[::paso].iloc[::-1]


def rango_eje(serie, rango, tope=None):
    """Eje vertical de `rango` unidades centrado en los datos (se agranda si no caben)."""
    bajo, alto = float(serie.min()), float(serie.max())
    centro = (bajo + alto) / 2
    amplitud = max(rango, (alto - bajo) + 4)
    lo, hi = centro - amplitud / 2, centro + amplitud / 2
    if tope is not None:
        if lo < tope[0]:
            lo, hi = tope[0], tope[0] + amplitud
        if hi > tope[1]:
            lo, hi = tope[1] - amplitud, tope[1]
    return lo, hi


def grafico(df, columna, titulo, color, limite=None, rango=RANGO_EJE, tope=None):
    """Línea suave con área, último valor destacado y (opcional) línea de límite.
    rango=None deja que el eje se ajuste solo a los datos (útil para el sustrato)."""
    df = adelgazar(df)

    if rango is None:
        escala = alt.Scale(zero=False)
    else:
        lo, hi = rango_eje(df[columna], rango, tope)
        escala = alt.Scale(domain=[lo, hi], nice=False, clamp=True)

    x = alt.X("fecha:T", title=None, axis=alt.Axis(format="%H:%M", grid=False))
    y = alt.Y(f"{columna}:Q", title=titulo, scale=escala)

    base = alt.Chart(df).encode(x=x, y=y)
    area = base.mark_area(opacity=0.15, color=color, interpolate="monotone", clip=True)
    linea = base.mark_line(color=color, strokeWidth=2.8, interpolate="monotone", clip=True)
    # Puntos invisibles: solo sirven para mostrar la hora y el valor al pasar el mouse
    hover = base.mark_circle(opacity=0, size=140).encode(
        tooltip=[
            alt.Tooltip("fecha:T", title="Hora", format="%d/%m %H:%M:%S"),
            alt.Tooltip(f"{columna}:Q", title=titulo, format=".1f"),
        ]
    )
    ultimo = alt.Chart(df.tail(1)).encode(x=x, y=y)
    punto = ultimo.mark_circle(color=color, size=120)
    etiqueta = ultimo.mark_text(
        align="right", dx=-10, dy=-14, fontWeight="bold", fontSize=13, color=color
    ).encode(text=alt.Text(f"{columna}:Q", format=".1f"))

    capas = area + linea + hover + punto + etiqueta

    if limite is not None:
        regla = (
            alt.Chart(pd.DataFrame({"y": [limite]}))
            .mark_rule(color="#e5484d", strokeDash=[6, 4], strokeWidth=2, clip=True)
            .encode(y=alt.Y("y:Q", scale=escala))
        )
        capas = capas + regla

    return capas.properties(height=280).configure_view(strokeWidth=0)


def estado_humedad(hum, limite):
    if hum > limite:
        return "🚨 Sobre el límite"
    if hum > limite - 5:
        return "⚠ Cerca del límite"
    return "✅ Dentro del límite"


# ============================ BARRA LATERAL =========================
with st.sidebar:
    st.header("⚙️ Ajustes")
    n_lecturas = st.slider("Lecturas a mostrar", 20, 500, 100, step=10)
    limite_global = st.slider("Límite de humedad (% RH)", 40, 90, 65)
    alerta_sonora = st.toggle("🔔 Alerta sonora", value=True)
    if st.button("🔄 Actualizar ahora", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption(f"Canal ThingSpeak: {CHANNEL_ID or 'sin configurar'}")
    st.caption(f"Actualización automática cada {REFRESCO}")

# ============================ ENCABEZADO ============================
st.markdown(
    """
    <div class="hero">
        <h1>🧪 Monitor Inteligente de Biomateriales</h1>
        <p>Prototipo de evaluación para etiquetas sustentables de ecommerce ·
        ESP32 + DHT11 + ThingSpeak</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# ============================ PANEL EN VIVO =========================
@st.fragment(run_every=REFRESCO)
def panel_en_vivo(resultados: int, limite: int, con_sonido: bool):
    df, error = leer_datos(resultados)

    if error:
        st.error(f"No se pudieron leer datos de ThingSpeak: {error}")
        return

    ultimo = df.iloc[-1]
    previo = df.iloc[-2] if len(df) > 1 else ultimo
    hum, temp = float(ultimo["hum"]), float(ultimo["temp"])

    # --- Alerta por humedad ---
    # El aviso rojo se muestra mientras dure la alerta; el toast y el sonido
    # suenan solo al ENTRAR en alerta, para que no se repitan cada 20 segundos.
    en_alerta = hum > limite
    estaba_en_alerta = st.session_state.get("alerta_activa", False)
    st.session_state["alerta_activa"] = en_alerta

    if en_alerta:
        st.error(
            f"🚨 **ALERTA:** La humedad actual es {hum:.1f}% RH, "
            f"por encima del límite de {limite}% RH."
        )
        if not estaba_en_alerta:
            st.toast(f"Humedad alta: {hum:.1f}% RH", icon="🚨")
            if con_sonido:
                st.markdown(
                    f"""
                    <audio autoplay>
                      <source src="data:audio/wav;base64,{sonido_alarma_b64()}" type="audio/wav">
                    </audio>
                    """,
                    unsafe_allow_html=True,
                )
    elif hum > limite - 5:
        st.warning(
            f"⚠ La humedad está cerca del límite: {hum:.1f}% RH "
            f"(límite: {limite}% RH)."
        )

    # ¿Siguen llegando datos?
    ahora = pd.Timestamp.now(tz=ZONA_HORARIA).tz_localize(None)
    minutos = (ahora - ultimo["fecha"]).total_seconds() / 60
    if minutos > MINUTOS_SIN_DATOS:
        st.warning(
            f"⏱ El último dato llegó hace {minutos:.0f} min. "
            "Revisa que el ESP32 esté encendido y con Wi-Fi."
        )
    else:
        st.success(f"🟢 Datos en vivo · canal {CHANNEL_ID} · {len(df)} lecturas cargadas")

    # --- Tarjetas ---
    etiqueta = estado_humedad(hum, limite)
    cols = st.columns(4 if "suelo" in df.columns else 3)

    cols[0].metric(
        "💧 Humedad del aire", f"{hum:.1f} % RH",
        f"{hum - float(previo['hum']):+.1f} vs. lectura anterior",
        delta_color="off",
    )
    cols[1].metric(
        "🌡️ Temperatura", f"{temp:.1f} °C",
        f"{temp - float(previo['temp']):+.1f} vs. lectura anterior",
        delta_color="off",
    )
    if "suelo" in df.columns and pd.notna(ultimo["suelo"]):
        cols[2].metric("🌱 Humedad del sustrato", f"{float(ultimo['suelo']):.1f}")
    cols[-1].metric("🕒 Última lectura", ultimo["fecha"].strftime("%H:%M:%S"), etiqueta, delta_color="off")

    # --- Gráficos ---
    st.subheader("📈 Historial ambiental")
    g1, g2 = st.columns(2)
    with g1:
        st.markdown("**Humedad del aire (% RH)** · línea roja = límite")
        st.altair_chart(
            grafico(df, "hum", "Humedad (% RH)", "#0ea5e9", limite, tope=(0, 100)),
            use_container_width=True,
        )
    with g2:
        st.markdown("**Temperatura (°C)**")
        st.altair_chart(grafico(df, "temp", "Temperatura (°C)", "#f97316"), use_container_width=True)

    if "suelo" in df.columns and df["suelo"].notna().any():
        st.markdown("**Humedad del sustrato**")
        st.altair_chart(grafico(df, "suelo", "Sustrato", "#16a34a", rango=None), use_container_width=True)

    # --- Muestras ---
    st.subheader("🧬 Comparación de muestras")

    filas = []
    for m in MUESTRAS:
        filas.append({
            "Muestra": m["Muestra"],
            "Material": m["Material base"],
            "Adhesivo": m["Adhesivo"],
            "Límite humedad": f"{m['Límite humedad (% RH)']} % RH",
            "Adhesión": m["Adhesión"],
            "Biodegradación": m["Biodegradación"],
            "Estado": estado_humedad(hum, m["Límite humedad (% RH)"]),
            "Próxima acción": m["Próxima acción"],
        })

    st.dataframe(
        pd.DataFrame(filas),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Muestra": st.column_config.TextColumn("Muestra", width="small"),
            "Material": st.column_config.TextColumn("Material base", width="medium"),
            "Adhesivo": st.column_config.TextColumn("Adhesivo", width="medium"),
            "Límite humedad": st.column_config.TextColumn("Límite", width="small"),
            "Adhesión": st.column_config.TextColumn("Adhesión", width="small"),
            "Biodegradación": st.column_config.TextColumn("Biodegradación", width="small"),
            "Estado": st.column_config.TextColumn(f"Estado a {hum:.0f}% RH", width="medium"),
            "Próxima acción": st.column_config.TextColumn("Próxima acción", width="large"),
        },
    )

    # --- Recomendación dinámica ---
    st.subheader("🤖 Recomendación preliminar")
    aptas = [m for m in MUESTRAS if hum <= m["Límite humedad (% RH)"]]
    if aptas:
        mejor = max(aptas, key=lambda m: m["Límite humedad (% RH)"] - hum)
        margen = mejor["Límite humedad (% RH)"] - hum
        st.success(
            f"Con {hum:.1f}% RH medido ahora, se prioriza **{mejor['Muestra']}** "
            f"({mejor['Material base']}): queda {margen:.1f} puntos bajo su límite. "
            "Esto no certifica el biomaterial: faltan ensayos de adhesión, "
            "resistencia al agua, imprimibilidad y biodegradabilidad."
        )
    else:
        st.warning(
            f"Con {hum:.1f}% RH ninguna muestra está dentro de su límite. "
            "Se recomienda revisar las muestras o repetir la medición."
        )

    st.caption(
        "El DHT11 tiene una precisión de ±5 % de humedad y ±2 °C: "
        "úsalo como referencia inicial, no como medición de precisión."
    )


panel_en_vivo(n_lecturas, limite_global, alerta_sonora)

# ============================ PREGUNTA ==============================
st.subheader("💬 Pregunta al asistente")
pregunta = st.text_input(
    "Escribe una pregunta sobre las mediciones o las muestras:",
    placeholder="Ejemplo: ¿Qué muestra conviene probar primero?",
)
if pregunta:
    st.info(
        "En una siguiente versión esta pregunta se enviará a Claude mediante el "
        "servidor MCP. Por ahora, haz la consulta directamente en Claude Desktop."
    )

st.divider()
st.caption(
    "Estado del prototipo: las mediciones vienen de ThingSpeak en tiempo real; "
    "los límites y las características de las muestras son valores de ejemplo."
)