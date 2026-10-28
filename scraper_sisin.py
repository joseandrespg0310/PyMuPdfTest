"""
Scraper SISIN - v5.2 (COMPLETO)

Mejoras incluidas:
- Path del boton PDF prioriza la estructura real de SISIN:
  //*[starts-with(@id,'ktablaRepor_')]/td/button[contains(@onclick,'dictamenImprime')]
- Fix critico: espera a que el onclick del boton PDF CAMBIE despues de clickear
  la fila (evita tomar el boton stale de la fila anterior).
- Click nativo Selenium primero (gesto de usuario real -> permite popup).
- Fallback: captura URL del onclick y navega manualmente.
- Extraccion de cobertura, poblacion beneficiaria y total beneficiarios.

Requisitos: pip install selenium pymupdf
"""

import base64
import csv
import hashlib
import json
import os
import re
import time
import traceback
from urllib.parse import urljoin

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ------------------------- CONFIGURACION -------------------------
BASE = "https://sisin.planificacion.gob.bo"
LOGIN_URL = f"{BASE}/bsn/ktr1/login/login/index.php"
PRINCIPAL_URL = f"{BASE}/bsn/ktr1/sbs_principal/mod_principal/principal.php"
DICTAMEN_PATH = "/bsn/ktr1/sbs_proyectoInversion/mod_dictamen/dictamen_vista.php"

PAGE_URL = (
    f"{BASE}{DICTAMEN_PATH}?ktbl_ktabla4Proy_page={{page}}"
    "&ktbl_ktabla4Proy_orderField=cod_inversion&ktbl_ktabla4Proy_orderDirec=0"
    "&ktbl_ktabla4Proy_search=&filterFieldktabla4Proy=&filterOpektabla4Proy="
    "&filterValuektabla4Proy="
)

ATTACH_CHROME = False
DEBUGGER_ADDRESS = "127.0.0.1:9222"
ESPERA_LOGIN = 30
HEADLESS = False

START_PAGE = 1
END_PAGE = 1

DIAGNOSTICAR_INICIAL = False
DIAGNOSTICAR_FILAS = (1, 2, 3)

PAUSA_ENTRE_FILAS = 1.0
PAUSA_ENTRE_PAGINAS = 2.0
TIMEOUT = 30
MAX_INTENTOS_FILA = 3

SAVE_PDFS = True
PDF_DIR = "pdfs"
OUT_CSV = "dictamenes_localizacion_beta.csv"
OUT_JSONL = "dictamenes_detalle_beta.jsonl"
DEBUG_DIR = "debug_extraccion"

COLUMNAS = [
    "pagina", "fila_id", "cod_inversion", "codigo_sisin", "nombre",
    "fecha_inicio", "fecha_fin", "clasificacion", "entidad", "estado", "tipo",
    "cod_dictamen", "cod_etapa", "url_pdf",
    "departamento", "provincia", "municipio",
    "nro_habitantes", "nro_habitantes_total",
    "cobertura", "pob_beneficiaria",
    "total_beneficiarios",
    "localizacion_json", "metodo_extraccion",
    "pdf_sha1", "pdf_menciona_codigo", "error_pdf",
]
# -----------------------------------------------------------------


# ============================ SELENIUM ============================
def crear_driver():
    opts = webdriver.ChromeOptions()
    if ATTACH_CHROME:
        opts.add_experimental_option("debuggerAddress", DEBUGGER_ADDRESS)
        return webdriver.Chrome(options=opts)
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1600,1000")
    return webdriver.Chrome(options=opts)


def login(driver, wait):
    if ATTACH_CHROME:
        print("[INFO] Usando Chrome ya abierto; se asume sesion iniciada")
    else:
        driver.get(LOGIN_URL)
        print(f"[INFO] Tienes {ESPERA_LOGIN} segundos para iniciar sesion...")
        time.sleep(ESPERA_LOGIN)
    driver.get(PRINCIPAL_URL)
    if "login" in driver.current_url.lower():
        raise SystemExit("[ERR] No se detecto sesion iniciada.")
    print("[OK] Sesion detectada:", driver.current_url)


def ir_a_dictamenes(driver, wait):
    link = wait.until(EC.element_to_be_clickable(
        (By.CSS_SELECTOR, f"a[href='{DICTAMEN_PATH}']")))
    link.click()
    wait.until(EC.presence_of_element_located(
        (By.ID, "ktabla4Proy_cboPageSelect")))
    print("[OK] En la pagina de Dictamenes")


def total_paginas(driver):
    return len(driver.find_elements(
        By.CSS_SELECTOR, "#ktabla4Proy_cboPageSelect option"))


def abrir_pagina(driver, wait, pagina):
    driver.get(PAGE_URL.format(page=pagina))
    wait.until(EC.presence_of_element_located((By.ID, "ktabla4Proy_1")))
    time.sleep(PAUSA_ENTRE_PAGINAS)


def estamos_en_lista(driver):
    try:
        return bool(driver.execute_script(
            "return !!document.getElementById('ktabla4Proy_cboPageSelect');"
        ))
    except Exception:
        return False


# ============ JS: buscar TODOS los elementos con el id ============
JS_INFO_FILA = r"""
var id = arguments[0];
var all = document.querySelectorAll('[id="' + id + '"]');
if (all.length === 0) return {count: 0, has_tr: false};

var tags = [];
var onclick = '';
var tds = 0;
var has_tr = false;
for (var i = 0; i < all.length; i++) {
    var el = all[i];
    tags.push(el.tagName);
    if (!onclick) {
        var oc = el.getAttribute('onclick') || '';
        if (oc) onclick = oc;
    }
    var innerTds = el.querySelectorAll('td').length;
    if (innerTds > tds) tds = innerTds;
    if (el.tagName === 'TR') has_tr = true;
    else if (!has_tr && el.querySelector('tr')) has_tr = true;
}
return {count: all.length, tags: tags, has_tr: has_tr,
        onclick: onclick, td_count: tds};
"""


def info_fila(driver, fila_id):
    return driver.execute_script(JS_INFO_FILA, fila_id) or {"count": 0}


def esperar_tr(driver, fila_id, timeout=15):
    fin = time.time() + timeout
    ultimo = None
    while time.time() < fin:
        try:
            info = info_fila(driver, fila_id)
        except Exception as e:
            ultimo = f"error JS: {e}"
            time.sleep(0.3)
            continue
        if info.get("has_tr"):
            return info
        ultimo = info
        time.sleep(0.3)
    print(f"      [WARN] esperar_tr agoto: {ultimo}")
    return None


JS_CLICK_FILA = r"""
var id = arguments[0];
var all = document.querySelectorAll('[id="' + id + '"]');
if (all.length === 0) return {status: 'no_element'};

var tr = null;
for (var i = 0; i < all.length; i++) {
    if (all[i].tagName === 'TR') { tr = all[i]; break; }
}
if (!tr) {
    for (var i = 0; i < all.length; i++) {
        var inner = all[i].querySelector('tr');
        if (inner) { tr = inner; break; }
    }
}
if (!tr) return {status: 'no_tr',
                 tags: Array.prototype.map.call(all, function(e){return e.tagName;})};

if (typeof tr.onclick === 'function') {
    try {
        tr.onclick.call(tr);
        return {status: 'onclick_ok', tag: tr.tagName};
    } catch (e) { }
}

var tds = tr.querySelectorAll('td');
if (tds.length >= 9) {
    try {
        tds[8].dispatchEvent(new MouseEvent(
            'click', {bubbles: true, cancelable: true, view: window}));
        return {status: 'td_click', td_count: tds.length};
    } catch (e) {}
}

try {
    tr.dispatchEvent(new MouseEvent(
        'click', {bubbles: true, cancelable: true, view: window}));
    return {status: 'tr_click', td_count: tds.length};
} catch (e) {
    return {status: 'err', error: String(e)};
}
"""


# ============ NUEVO: leer onclick del boton PDF ============
JS_GET_BOTON_ONCLICK = r"""
var btns = document.querySelectorAll('button[onclick*="dictamenImprime"]');
if (!btns.length) return null;
return btns[0].getAttribute('onclick');
"""


def onclick_boton_actual(driver):
    """Devuelve el onclick del primer boton dictamenImprime, o None."""
    try:
        return driver.execute_script(JS_GET_BOTON_ONCLICK)
    except Exception:
        return None


# ============ LECTURA DE DATOS DE FILA ============
JS_LEER_FILA = r"""
var id = arguments[0];
var all = document.querySelectorAll('[id="' + id + '"]');
if (all.length === 0) return null;

var tr = null;
for (var i = 0; i < all.length; i++) {
    if (all[i].tagName === 'TR') { tr = all[i]; break; }
}
if (!tr) {
    for (var i = 0; i < all.length; i++) {
        var inner = all[i].querySelector('tr');
        if (inner) { tr = inner; break; }
    }
}
if (!tr) tr = all[0];

var tds = tr.querySelectorAll('td');
var celdas = [];
for (var i = 0; i < tds.length; i++) {
    celdas.push((tds[i].innerText || '').trim());
}
return {tag: tr.tagName,
        onclick: tr.getAttribute('onclick') || '',
        celdas: celdas};
"""


def leer_fila_por_id(driver, wait, fila_id):
    wait.until(EC.presence_of_element_located((By.ID, fila_id)))
    info = driver.execute_script(JS_LEER_FILA, fila_id)
    if not info:
        raise RuntimeError(f"{fila_id} desaparecio")

    onclick = info.get("onclick") or ""
    m = re.search(r"inversion_ver\('(\d+)'", onclick)

    celdas = list(info.get("celdas") or [])
    celdas += [""] * (10 - len(celdas))

    return {
        "fila_id": fila_id,
        "cod_inversion": m.group(1) if m else "",
        "codigo_sisin": celdas[0],
        "nombre": celdas[1],
        "fecha_inicio": celdas[2],
        "fecha_fin": celdas[3],
        "clasificacion": celdas[4],
        "entidad": celdas[5],
        "estado": celdas[6],
        "tipo": celdas[7],
    }


# ===================== DUMP DE DEBUG =====================
def _dump_debug(driver, fila_id, motivo="sin_boton"):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    base = os.path.join(DEBUG_DIR, f"{motivo}_{fila_id}")
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    try:
        with open(base + "_main.html", "w", encoding="utf-8") as fp:
            fp.write(driver.page_source)
    except Exception as e:
        print(f"      [WARN] dump main: {e}")

    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except Exception:
        frames = []

    for i in range(len(frames)):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(i)
            with open(base + f"_iframe_{i}.html", "w", encoding="utf-8") as fp:
                fp.write(driver.page_source)
        except Exception as e:
            print(f"      [WARN] dump iframe {i}: {e}")
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    print(f"      [DEBUG] HTML guardado en: {base}_*.html")
    return base


# ============================================================
# ============ BOTON PDF (path mejorado) ====================
# ============================================================
def _attrs_pdf(el):
    try:
        return {
            "onclick": (el.get_attribute("onclick") or "").lower(),
            "title":   (el.get_attribute("title")   or "").lower(),
            "alt":     (el.get_attribute("alt")     or "").lower(),
            "src":     (el.get_attribute("src")     or "").lower(),
            "href":    (el.get_attribute("href")    or "").lower(),
            "value":   (el.get_attribute("value")   or "").lower(),
            "texto":   (el.text                     or "").lower(),
            "clase":   (el.get_attribute("class")   or "").lower(),
            "id":      (el.get_attribute("id")      or "").lower(),
        }
    except (StaleElementReferenceException, Exception):
        return None


def _es_boton_pdf(el):
    attrs = _attrs_pdf(el)
    if not attrs:
        return False
    # Prioridad 1: onclick con dictamenImprime
    if "dictamenimprime" in attrs["onclick"]:
        return True
    try:
        tag = el.tag_name.lower()
    except Exception:
        return False
    if tag == "img":
        return False
    if tag == "button":
        if "imprimir" in attrs["title"] or "pdf" in attrs["title"]:
            return True
    if tag not in ("button", "a", "input", "span", "i", "div"):
        return False
    blob = " ".join(attrs.values())
    if any(w in blob for w in ("pdf", "imprimir", "print")):
        return True
    if tag == "a" and (".pdf" in attrs["href"] or "dictamen" in attrs["href"]):
        return True
    return False


def _subir_a_clickeable(el, max_niveles=4):
    actual = el
    for _ in range(max_niveles):
        try:
            tag = actual.tag_name.lower()
            if tag == "img":
                src = (actual.get_attribute("src") or "").lower()
                if any(w in src for w in ("printer", "print", "pdf", "imprimir")):
                    actual = actual.find_element(By.XPATH, "..")
                    continue
            if _es_boton_pdf(actual):
                return actual
            actual = actual.find_element(By.XPATH, "..")
        except Exception:
            return None
    return None


def _iter_candidatos_contexto(driver):
    vistos = set()
    busquedas = [
        (By.XPATH,
         "//*[starts-with(@id,'ktablaRepor_')]/td/button"
         "[contains(@onclick,'dictamenImprime')]"),
        (By.XPATH,
         "//*[starts-with(@id,'ktablaRepor_')]//button"
         "[contains(@onclick,'dictamenImprime')]"),
        (By.CSS_SELECTOR, "button.kbuttonImg[onclick*='dictamenImprime']"),
        (By.CSS_SELECTOR, "button[onclick*='dictamenImprime']"),
        (By.CSS_SELECTOR, "[onclick*='dictamenImprime']"),
        (By.XPATH,
         "//button[contains(@title,'Imprimir') or contains(@title,'imprimir')]"),
        (By.XPATH, "//button[.//img[contains(@src,'printer')]]"),
        (By.CSS_SELECTOR, "button.kbuttonImg"),
        (By.CSS_SELECTOR, "button"),
        (By.CSS_SELECTOR, "a"),
        (By.CSS_SELECTOR, "img"),
        (By.CSS_SELECTOR, "input"),
        (By.CSS_SELECTOR, "[onclick]"),
    ]
    for by, sel in busquedas:
        try:
            elementos = driver.find_elements(by, sel)
        except Exception:
            continue
        for el in elementos:
            try:
                eid = el.id
            except Exception:
                continue
            if eid in vistos:
                continue
            vistos.add(eid)
            yield el


def _iter_candidatos_en(contenedor):
    vistos = set()
    busquedas = [
        ".//*[starts-with(@id,'ktablaRepor_')]/td/button"
        "[contains(@onclick,'dictamenImprime')]",
        ".//*[starts-with(@id,'ktablaRepor_')]//button"
        "[contains(@onclick,'dictamenImprime')]",
        ".//button[contains(@onclick, 'dictamenImprime')]",
        ".//*[contains(@onclick, 'dictamenImprime')]",
        ".//button[contains(@title, 'Imprimir') or contains(@title, 'imprimir')]",
        ".//button[.//img[contains(@src,'printer')]]",
        ".//button", ".//a", ".//img", ".//input", ".//*[@onclick]",
    ]
    for sel in busquedas:
        try:
            elementos = contenedor.find_elements(By.XPATH, sel)
        except Exception:
            continue
        for el in elementos:
            try:
                eid = el.id
            except Exception:
                continue
            if eid in vistos:
                continue
            vistos.add(eid)
            yield el


def _buscar_en_contexto(driver):
    for el in _iter_candidatos_contexto(driver):
        c = _subir_a_clickeable(el)
        if c is not None:
            return c
    return None


def encontrar_boton_pdf(driver, wait):
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Atajo 1: path exacto de SISIN
    try:
        candidatos = driver.find_elements(
            By.XPATH,
            "//*[starts-with(@id,'ktablaRepor_')]/td/button"
            "[contains(@onclick,'dictamenImprime')]")
        if not candidatos:
            candidatos = driver.find_elements(
                By.XPATH,
                "//*[starts-with(@id,'ktablaRepor_')]//button"
                "[contains(@onclick,'dictamenImprime')]")
        if candidatos:
            for c in candidatos:
                try:
                    title = (c.get_attribute("title") or "").lower()
                    if "imprimir" in title or "firma" in title:
                        return c
                except StaleElementReferenceException:
                    continue
            try:
                return candidatos[0]
            except StaleElementReferenceException:
                pass
    except Exception:
        pass

    # Atajo 2: cualquier button con dictamenImprime
    try:
        candidatos = driver.find_elements(
            By.XPATH, "//button[contains(@onclick,'dictamenImprime')]")
        if candidatos:
            return candidatos[0]
    except Exception:
        pass

    # Fallback general
    try:
        b = _buscar_en_contexto(driver)
        if b is not None:
            return b
    except Exception:
        pass

    try:
        cont = driver.find_element(By.ID, "contiene_datos_centro")
        for el in _iter_candidatos_en(cont):
            c = _subir_a_clickeable(el)
            if c is not None:
                return c
    except Exception:
        pass

    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except Exception:
        frames = []

    for i in range(len(frames)):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(i)
            b = _buscar_en_contexto(driver)
            if b is not None:
                return b
        except Exception:
            pass
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    return None


def esperar_boton_pdf(driver, wait, timeout=30, intervalo=0.6):
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            b = encontrar_boton_pdf(driver, wait)
            if b is not None:
                return b
        except StaleElementReferenceException:
            pass
        except Exception:
            pass
        time.sleep(intervalo)
    return None


# =========== CAPTURA DE URL DEL ONCLICK (FALLBACK) ===========
JS_CAPTURAR_URL = r"""
var btn = arguments[0];
if (!btn) return {url: null, error: 'sin elemento'};

var capturedUrl = null;
var originalOpen = window.open;
window.open = function(url, name, features) {
    capturedUrl = url;
    return null;
};

var err = null;
try {
    var oc = btn.getAttribute('onclick');
    if (oc) {
        (new Function(oc)).call(btn);
    } else if (typeof btn.onclick === 'function') {
        btn.onclick.call(btn);
    } else {
        btn.click();
    }
} catch (e) {
    err = String(e);
} finally {
    window.open = originalOpen;
}

return {url: capturedUrl, error: err};
"""


def _esperar_nueva_pestana(driver, antes, timeout=8):
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            handles = driver.window_handles
        except Exception:
            break
        if len(handles) > len(antes):
            nuevas = [h for h in handles if h not in antes]
            if nuevas:
                return nuevas[-1]
        time.sleep(0.3)
    return None


def _procesar_pestana_pdf(driver, nueva, ventana_original):
    try:
        driver.switch_to.window(nueva)
    except Exception as e:
        raise RuntimeError(f"switch a nueva pestana fallo: {e}")

    try:
        WebDriverWait(driver, 30).until(
            lambda d: (
                "dictamen" in (d.current_url or "").lower()
                or ".pdf" in (d.current_url or "").lower()
                or "reporte" in (d.current_url or "").lower()
                or "download" in (d.current_url or "").lower()
            )
        )
        return driver.current_url
    except TimeoutException:
        cur = driver.current_url
        if cur and cur != "about:blank":
            return cur
        raise RuntimeError("URL del PDF no detectada")


# ===================== CLICK Y NUEVA PESTANA =====================
def click_boton_pdf_webdriver(driver, boton, wait, timeout=30):
    try:
        ventana_original = driver.current_window_handle
        antes = set(driver.window_handles)
    except Exception as e:
        raise RuntimeError(f"Error obteniendo handles: {e}")

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});",
            boton)
        time.sleep(0.5)
    except Exception as e:
        print(f"      [WARN] scrollIntoView fallo: {e}")

    # Intento 1: click nativo Selenium (gesto de usuario real)
    click_nativo_ok = False
    try:
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable(boton))
        boton.click()
        print("      [OK] Click nativo Selenium")
        click_nativo_ok = True
    except (ElementClickInterceptedException,
            ElementNotInteractableException,
            StaleElementReferenceException,
            TimeoutException) as e:
        print(f"      [WARN] Click nativo fallo: {type(e).__name__}: {e}")
    except WebDriverException as e:
        print(f"      [WARN] Click nativo fallo (WebDriver): {e}")

    nueva = _esperar_nueva_pestana(driver, antes, timeout=10)
    if nueva is not None:
        url_pdf = _procesar_pestana_pdf(driver, nueva, ventana_original)
        print(f"      [OK] PDF en nueva pestana: {url_pdf}")
        try:
            driver.switch_to.window(ventana_original)
        except Exception:
            pass
        return url_pdf, ventana_original

    # Intento 2: JS click
    if not click_nativo_ok:
        try:
            driver.execute_script("arguments[0].click();", boton)
            print("      [OK] Click JS (fallback)")
            nueva = _esperar_nueva_pestana(driver, antes, timeout=8)
            if nueva is not None:
                url_pdf = _procesar_pestana_pdf(driver, nueva, ventana_original)
                print(f"      [OK] PDF en nueva pestana: {url_pdf}")
                try:
                    driver.switch_to.window(ventana_original)
                except Exception:
                    pass
                return url_pdf, ventana_original
        except Exception as e:
            print(f"      [WARN] Click JS fallo: {e}")

    # Intento 3: capturar URL del onclick
    print("      [WARN] Sin popup. Capturando URL del onclick...")
    try:
        result = driver.execute_script(JS_CAPTURAR_URL, boton)
    except Exception as e:
        raise RuntimeError(f"Captura de URL fallo: {e}")

    if not result or not result.get("url"):
        raise RuntimeError(
            f"No se pudo capturar URL del PDF (result={result})")

    url_raw = result["url"]
    url_abs = urljoin(driver.current_url or "", url_raw)
    print(f"      [OK] URL capturada: {url_abs}")

    try:
        driver.switch_to.new_window('tab')
        driver.get(url_abs)
        url_pdf = driver.current_url
        print(f"      [OK] PDF navegado manualmente: {url_pdf}")
        try:
            driver.switch_to.window(ventana_original)
        except Exception:
            pass
        return url_pdf, ventana_original
    except Exception as e:
        try:
            driver.close()
        except Exception:
            pass
        try:
            driver.switch_to.window(ventana_original)
        except Exception:
            pass
        raise RuntimeError(f"No se pudo navegar a la URL del PDF: {e}")


# =============================================================
# ============ FLUJO DE LA FILA (FIX PRINCIPAL) ===============
# =============================================================
def abrir_detalle_y_pdf(driver, wait, fila_id, datos=None, pagina=None,
                        max_intentos=MAX_INTENTOS_FILA):
    """
    FIX CRITICO: espera a que el onclick del boton PDF CAMBIE despues de
    clickear la fila. Evita tomar el boton stale de la fila anterior antes
    de que el AJAX del detalle haya terminado.
    """
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    ultimo_error = None

    for intento in range(1, max_intentos + 1):
        print(f"      [INTENTO {intento}/{max_intentos}] {fila_id}")

        # Cerrar pestanas extra que hayan quedado abiertas
        try:
            handles = driver.window_handles
            if len(handles) > 1:
                for h in handles[1:]:
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
                driver.switch_to.window(handles[0])
        except Exception:
            pass

        # En reintentos, recargar la pagina para limpiar el estado
        if intento > 1 and pagina is not None:
            print(f"      [INFO] Recargando pagina {pagina}...")
            try:
                driver.get(PAGE_URL.format(page=pagina))
                wait.until(EC.presence_of_element_located(
                    (By.ID, "ktabla4Proy_1")))
                time.sleep(1.0)
            except Exception as e:
                print(f"      [WARN] reload fallo: {e}")

        info = esperar_tr(driver, fila_id, timeout=15)
        if info is None:
            ultimo_error = RuntimeError(f"{fila_id} nunca tuvo <tr>")
            time.sleep(1)
            continue

        # *** CAPTURAR ONCLICK ANTES DE CLICKEAR LA FILA ***
        onclick_antes = onclick_boton_actual(driver)
        if onclick_antes:
            print(f"      [INFO] Boton previo: {onclick_antes[:70]}")
        else:
            print(f"      [INFO] No habia boton previo")

        print(f"      [INFO] {fila_id} listo tags={info.get('tags')} "
              f"tds={info.get('td_count')}")

        try:
            result = driver.execute_script(JS_CLICK_FILA, fila_id)
        except Exception as e:
            ultimo_error = e
            print(f"      [WARN] JS click fallo: {e}")
            time.sleep(1)
            continue

        print(f"      [INFO] Click fila: {result}")

        if not result or result.get("status") in ("no_element", "no_tr", "err"):
            ultimo_error = RuntimeError(f"Click no ejecutado: {result}")
            time.sleep(1)
            continue

        # *** ESPERAR A QUE EL ONCLICK CAMBIE ***
        boton = None
        fin = time.time() + 30
        while time.time() < fin:
            onclick_actual = onclick_boton_actual(driver)

            cambio = False
            if onclick_antes is None and onclick_actual is not None:
                cambio = True
                print(f"      [OK] Boton aparecio: {onclick_actual[:70]}")
            elif (onclick_antes is not None and onclick_actual is not None
                    and onclick_actual != onclick_antes):
                cambio = True
                print(f"      [OK] Boton cambio: {onclick_actual[:70]}")

            if cambio:
                time.sleep(0.3)
                boton = encontrar_boton_pdf(driver, wait)
                if boton is not None:
                    break

            time.sleep(0.5)

        if boton is None:
            print("      [WARN] Boton PDF no cambio/aparecio en 30s")
            _dump_debug(driver, fila_id, motivo=f"sin_boton_int{intento}")
            ultimo_error = RuntimeError(
                f"Boton PDF no cambio para {fila_id} "
                f"(antes={str(onclick_antes)[:40]})")
            time.sleep(2)
            continue

        print("      [OK] Boton PDF listo")
        try:
            return click_boton_pdf_webdriver(driver, boton, wait)
        except Exception as e:
            ultimo_error = e
            print(f"      [WARN] click PDF fallo: {e}")
            time.sleep(1)
            continue

    if ultimo_error is not None:
        _dump_debug(driver, fila_id, motivo="fallo_final")
    raise ultimo_error or RuntimeError(f"No se pudo procesar {fila_id}")


# ===================== DIAGNOSTICO =====================
def diagnosticar_fila(driver, wait, fila_id):
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    print(f"\n{'=' * 60}\nDIAGNOSTICO: {fila_id}\n{'=' * 60}")

    try:
        wait.until(EC.presence_of_element_located((By.ID, fila_id)))
    except TimeoutException:
        print(f"  [ERR] No existe {fila_id}")
        print(f"{'=' * 60}\n")
        return

    info = driver.execute_script("""
        var id = arguments[0];
        var all = document.querySelectorAll('[id="' + id + '"]');
        var out = {count: all.length, elementos: []};
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            var rect = el.getBoundingClientRect();
            out.elementos.push({
                tag: el.tagName,
                cls: el.className || '',
                onclick: (el.getAttribute('onclick') || '').substring(0, 120),
                visible: rect.width > 0 && rect.height > 0,
                tds: el.querySelectorAll('td').length,
                html500: (el.outerHTML || '').substring(0, 500)
            });
        }
        return out;
    """, fila_id)

    print(f"  Elementos con id='{fila_id}': {info.get('count')}")
    for i, el in enumerate(info.get("elementos") or []):
        print(f"\n  --- elemento [{i}] ---")
        print(f"    tag        : {el.get('tag')}")
        print(f"    class      : {el.get('cls')}")
        print(f"    onclick    : {el.get('onclick')}")
        print(f"    visible    : {el.get('visible')}")
        print(f"    td_count   : {el.get('tds')}")

    try:
        n = driver.execute_script(
            "var c=document.getElementById('contiene_datos_centro');"
            "return c ? c.innerHTML.length : -1;")
        print(f"\n  #contiene_datos_centro: "
              f"{'EXISTE len=' + str(n) if n >= 0 else 'NO EXISTE'}")
    except Exception:
        pass

    print(f"{'=' * 60}\n")


# ===================== DESCARGA DEL PDF =====================
JS_FETCH = """
const url = arguments[0];
const cb = arguments[arguments.length - 1];
fetch(url, {credentials: 'include', cache: 'no-store'})
  .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
  .then(b => {
      const fr = new FileReader();
      fr.onloadend = () => cb(fr.result.split(',')[1]);
      fr.readAsDataURL(b);
  })
  .catch(e => cb('ERR:' + e));
"""


def descargar_pdf(driver, url):
    driver.set_script_timeout(120)
    try:
        b64 = driver.execute_async_script(JS_FETCH, url)
    except Exception as e:
        raise RuntimeError(f"fetch JS fallo: {e}")
    if not b64 or b64.startswith("ERR:"):
        raise RuntimeError(b64 or "respuesta vacia")
    try:
        data = base64.b64decode(b64)
    except Exception as e:
        raise RuntimeError(f"base64 invalido: {e}")
    if not data.startswith(b"%PDF"):
        raise RuntimeError("La respuesta no es un PDF")
    return data


# ============================ PYMUPDF ============================
def _norm(x):
    return re.sub(r"\s+", " ", (x or "")).strip()


def _num_entero(t):
    d = re.sub(r"[^\d]", "", _norm(t))
    return d if len(d) >= 3 else ""


def _num_entero_libre(t):
    d = re.sub(r"[^\d]", "", _norm(t))
    return d if d else ""


def _num_decimal(t):
    return _norm(t).replace(" ", "")


def _texto_celda(page, bbox):
    rect = fitz.Rect(bbox)
    try:
        raw = page.get_text("rawdict", clip=rect)
        lineas = []
        for b in raw.get("blocks", []):
            for ln in b.get("lines", []):
                chars = []
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        x0, y0, x1, y1 = ch["bbox"]
                        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                        if rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1:
                            chars.append((x0, x1, ch["c"], sp.get("size", 10) or 10))
                if not chars:
                    continue
                chars.sort(key=lambda c: c[0])
                txt = chars[0][2]
                for prev, cur in zip(chars, chars[1:]):
                    if (cur[0] - prev[1] > 0.15 * cur[3]
                            and not txt.endswith(" ") and cur[2] != " "):
                        txt += " "
                    txt += cur[2]
                lineas.append(txt)
        t = _norm(" ".join(lineas))
        if t:
            return t
    except Exception:
        pass
    return _norm(page.get_text("text", clip=rect))


def _normalizar_header(t):
    t = _norm(t).lower()
    t = re.sub(r"[^a-z0-9. ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _mapear_columnas(headers):
    h = [_normalizar_header(x) for x in headers]
    idx = {}

    def buscar(patron):
        for i, texto in enumerate(h):
            if re.search(patron, texto):
                return i
        return None

    idx["departamento"] = buscar(r"departamento")
    idx["provincia"]    = buscar(r"provincia")
    idx["municipio"]    = buscar(r"municipio|localidad")
    idx["habitantes"]   = buscar(r"habitantes")
    idx["cobertura"]    = buscar(r"cobertura")
    idx["beneficiaria"] = buscar(r"benef")

    if any(idx[k] is None
           for k in ("departamento", "provincia", "municipio", "habitantes")):
        return None
    return idx


def _extraer_por_find_tables(doc):
    filas = []
    for page in doc:
        try:
            tables = page.find_tables().tables
        except Exception:
            continue
        for tabla in tables:
            try:
                data = [[_texto_celda(page, c) if c else "" for c in row.cells]
                        for row in tabla.rows]
            except Exception:
                try:
                    data = tabla.extract()
                except Exception:
                    continue
            if not data:
                continue
            header_idx, mapa = None, None
            for i, row in enumerate(data):
                mapa = _mapear_columnas(row)
                if mapa:
                    header_idx = i
                    break
            if not mapa:
                continue
            for row in data[header_idx + 1:]:
                f = [_norm(x) for x in row]
                if not f or not re.fullmatch(r"\d+", f[0] or ""):
                    continue

                def valor(campo):
                    i = mapa.get(campo)
                    return f[i] if i is not None and i < len(f) else ""

                dep, mun = valor("departamento"), valor("municipio")
                hab = _num_entero(valor("habitantes"))
                if not dep or not mun or not hab:
                    continue
                filas.append({
                    "departamento": dep,
                    "provincia": valor("provincia"),
                    "municipio": mun,
                    "nro_habitantes": hab,
                    "cobertura": _num_decimal(valor("cobertura")),
                    "pob_beneficiaria": _num_entero_libre(valor("beneficiaria")),
                })
            if filas:
                return filas, "find_tables"
    return [], None


def _agrupar_por_linea(words, tolerancia=4):
    lineas = {}
    for w in words:
        y = (w[1] + w[3]) / 2
        clave = None
        for ye in lineas:
            if abs(ye - y) <= tolerancia:
                clave = ye
                break
        if clave is None:
            clave = y
            lineas[clave] = []
        lineas[clave].append(w)
    return lineas


def _extraer_por_coordenadas(doc):
    filas = []
    for page in doc:
        words = page.get_text("words", sort=True)
        if not words:
            continue
        titulo = [w for w in words
                  if "LOCALIZACI" in w[4].upper() and "GEOGR" in w[4].upper()]
        if not titulo:
            continue
        y_titulo = min(w[1] for w in titulo)
        palabras = [w for w in words if w[1] > y_titulo]

        def buscar(regex):
            c = [w for w in palabras if re.search(regex, w[4], re.IGNORECASE)]
            return min(c, key=lambda w: w[1]) if c else None

        h_dep = buscar(r"^DEPARTAMENTO$")
        h_prov = buscar(r"^PROVINCIA$")
        h_mun = buscar(r"^(MUNICIPIO|LOCALIDAD)$")
        h_hab = buscar(r"^HABITANTES$")
        h_cob = buscar(r"COBERTURA")
        h_ben = buscar(r"POB|BENEF")
        if not all([h_dep, h_prov, h_mun, h_hab]):
            continue
        y_header = min(w[3] for w in [h_dep, h_prov, h_mun, h_hab])
        centros = {
            "departamento": (h_dep[0] + h_dep[2]) / 2,
            "provincia":    (h_prov[0] + h_prov[2]) / 2,
            "municipio":    (h_mun[0] + h_mun[2]) / 2,
            "habitantes":   (h_hab[0] + h_hab[2]) / 2,
        }
        if h_cob:
            centros["cobertura"] = (h_cob[0] + h_cob[2]) / 2
        if h_ben:
            centros["beneficiaria"] = (h_ben[0] + h_ben[2]) / 2
        xs = sorted(centros.values())
        anchos = [(b - a) / 2 for a, b in zip(xs, xs[1:])]
        ancho_col = min(anchos) if anchos else 60
        total_words = [w for w in palabras
                       if w[4].upper().startswith("TOTAL") and w[1] > y_header]
        y_fin = min(w[1] for w in total_words) if total_words else page.rect.y1
        datos = [w for w in palabras if y_header < w[1] < y_fin]
        lineas = _agrupar_por_linea(datos)
        for y in sorted(lineas):
            linea = sorted(lineas[y], key=lambda w: w[0])
            fila = {}
            for campo, centro in centros.items():
                cand = [w for w in linea
                        if abs((w[0] + w[2]) / 2 - centro) <= ancho_col]
                if cand:
                    cand.sort(key=lambda w: w[0])
                    fila[campo] = " ".join(w[4] for w in cand)
                else:
                    fila[campo] = ""
            nro = next((w[4] for w in linea if re.fullmatch(r"\d+", w[4])), None)
            hab = _num_entero(fila.get("habitantes", ""))
            if nro is None or not hab:
                continue
            if not fila.get("departamento") or not fila.get("municipio"):
                continue
            filas.append({
                "departamento": _norm(fila.get("departamento", "")),
                "provincia":    _norm(fila.get("provincia", "")),
                "municipio":    _norm(fila.get("municipio", "")),
                "nro_habitantes": hab,
                "cobertura":      _num_decimal(fila.get("cobertura", "")),
                "pob_beneficiaria": _num_entero_libre(fila.get("beneficiaria", "")),
            })
        if filas:
            return filas, "coordenadas"
    return [], None


def extraer_total_beneficiarios(doc):
    for page in doc:
        try:
            texto = page.get_text("text")
        except Exception:
            continue
        m = re.search(r"Total\s+Beneficiarios[^\d]*?(\d+)",
                      texto, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def extraer_localizacion(pdf_bytes, cod_inversion="", codigo_sisin=""):
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return [], "", f"no se pudo abrir PDF: {e}", ""

    try:
        filas, metodo = _extraer_por_find_tables(doc)
        if not filas:
            filas, metodo = _extraer_por_coordenadas(doc)

        total_benef = extraer_total_beneficiarios(doc)

        validas = [f for f in filas
                   if f.get("nro_habitantes")
                   and f.get("departamento")
                   and f.get("municipio")]
        if not validas:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            base = os.path.join(DEBUG_DIR, f"{cod_inversion}_{codigo_sisin}")
            try:
                with open(base + ".txt", "w", encoding="utf-8") as fp:
                    for page in doc:
                        fp.write(page.get_text("text"))
                        fp.write("\n\n----- FIN PAGINA -----\n\n")
            except Exception:
                pass
            return [], metodo or "", "sin filas validas", total_benef

        return validas, metodo or "", "", total_benef

    except Exception as e:
        return [], "", f"error PyMuPDF: {e}", ""
    finally:
        try:
            doc.close()
        except Exception:
            pass


def codigo_en_pdf(pdf_bytes, codigo_sisin):
    codigo = re.sub(r"\D", "", codigo_sisin or "")
    if not codigo:
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ""
    try:
        digitos = re.sub(r"\D", "", "".join(p.get_text() for p in doc))
        return "si" if codigo in digitos else "no"
    except Exception:
        return ""
    finally:
        try:
            doc.close()
        except Exception:
            pass


def aplanar_localizacion(filas, total_beneficiarios=""):
    campos = ["departamento", "provincia", "municipio",
              "nro_habitantes", "cobertura", "pob_beneficiaria"]
    out = {c: " | ".join(f.get(c, "") for f in filas) for c in campos}

    nums_hab, nums_ben = [], []
    for f in filas:
        h = _num_entero(f.get("nro_habitantes", ""))
        if h:
            nums_hab.append(int(h))
        b = _num_entero_libre(f.get("pob_beneficiaria", ""))
        if b:
            nums_ben.append(int(b))

    out["nro_habitantes_total"] = sum(nums_hab) if nums_hab else ""
    out["total_beneficiarios"] = (
        total_beneficiarios if total_beneficiarios
        else (sum(nums_ben) if nums_ben else "")
    )
    out["localizacion_json"] = json.dumps(filas, ensure_ascii=False)
    return out


# ============================== MAIN ==============================
def ya_procesados():
    vistos = set()
    if not os.path.exists(OUT_CSV):
        return vistos
    try:
        with open(OUT_CSV, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("error_pdf") or "").strip():
                    continue
                if not (r.get("nro_habitantes") or "").strip():
                    continue
                cod = (r.get("cod_inversion") or "").strip()
                if cod:
                    vistos.add(cod)
    except Exception as e:
        print(f"[WARN] no se pudo leer {OUT_CSV}: {e}")
    return vistos


def cerrar_pestanas_extra(driver, ventana_original):
    try:
        handles = list(driver.window_handles)
    except Exception:
        return
    for h in handles:
        if h != ventana_original:
            try:
                driver.switch_to.window(h)
                driver.close()
            except Exception:
                pass
    try:
        driver.switch_to.window(ventana_original)
    except Exception:
        pass


def mostrar_extraccion(datos, url_pdf, metodo, loc, total_benef=""):
    print("\n      ============ DATOS EXTRAIDOS ============")
    print(f"      Metodo           : {metodo}")
    print(f"      Codigo SISIN     : {datos['codigo_sisin']}")
    print(f"      Proyecto         : {datos['nombre'][:60]}")
    print(f"      PDF              : {url_pdf}")
    print("      -----------------------------------------")
    for n, f in enumerate(loc, start=1):
        print(f"      Loc {n}: {f.get('departamento','')} / "
              f"{f.get('provincia','')} / {f.get('municipio','')}")
        print(f"        Nro. Habitantes   : {f.get('nro_habitantes','')}")
        print(f"        Cobertura         : {f.get('cobertura','')}")
        print(f"        Pob. Beneficiaria : {f.get('pob_beneficiaria','')}")

    total_hab = sum(int(f["nro_habitantes"]) for f in loc
                    if str(f.get("nro_habitantes", "")).isdigit())
    print(f"      TOTAL Habitantes   : {total_hab}")
    print(f"      TOTAL Beneficiarios: {total_benef}")
    print("      ==========================================\n")


def main():
    if SAVE_PDFS:
        os.makedirs(PDF_DIR, exist_ok=True)

    driver = crear_driver()
    wait = WebDriverWait(driver, TIMEOUT)
    vistos = ya_procesados()

    nuevo_csv = not os.path.exists(OUT_CSV)
    f_csv = open(OUT_CSV, "a", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(f_csv, fieldnames=COLUMNAS, restval="")
    if nuevo_csv:
        writer.writeheader()
    f_json = open(OUT_JSONL, "a", encoding="utf-8")

    try:
        login(driver, wait)
        ir_a_dictamenes(driver, wait)
        ultima = END_PAGE or total_paginas(driver)
        print(f"\n[INFO] MODO BETA: pagina {START_PAGE} a {ultima}")

        if DIAGNOSTICAR_INICIAL:
            for n in DIAGNOSTICAR_FILAS:
                try:
                    diagnosticar_fila(driver, wait, f"ktabla4Proy_{n}")
                except Exception as e:
                    print(f"[WARN] diag fila {n} fallo: {e}")

        for pagina in range(START_PAGE, ultima + 1):
            print(f"\n[PAG {pagina}/{ultima}] Cargando pagina...")
            abrir_pagina(driver, wait, pagina)

            for numero_fila in range(1, 31):
                fila_id = f"ktabla4Proy_{numero_fila}"

                if not estamos_en_lista(driver):
                    print("   [WARN] Lista perdida, recargando pagina...")
                    abrir_pagina(driver, wait, pagina)

                try:
                    datos = leer_fila_por_id(driver, wait, fila_id)
                except TimeoutException:
                    print(f"   [INFO] No existe {fila_id}; fin de pagina")
                    break
                except Exception as e:
                    print(f"   [ERR] lectura {fila_id}: {e}")
                    break

                if datos["cod_inversion"] in vistos:
                    print(f"   [SKIP] {fila_id} | {datos['codigo_sisin']} | "
                          f"ya procesado")
                    continue

                print(f"\n   [FILA {numero_fila}] {datos['codigo_sisin']} - "
                      f"{datos['nombre'][:70]}")

                fila_csv = {**datos, "pagina": pagina, "error_pdf": ""}
                url_pdf = ""
                try:
                    ventana_original = driver.current_window_handle
                except Exception:
                    ventana_original = None

                try:
                    # FIX: pasar 'pagina' para permitir reload en retry
                    url_pdf, ventana_original = abrir_detalle_y_pdf(
                        driver, wait, fila_id, datos, pagina=pagina)
                    fila_csv["url_pdf"] = url_pdf

                    pdf = descargar_pdf(driver, url_pdf)

                    if SAVE_PDFS:
                        nombre_pdf = (f"{datos['cod_inversion']}_"
                                      f"{datos['codigo_sisin']}.pdf")
                        try:
                            with open(os.path.join(PDF_DIR, nombre_pdf), "wb") as fp:
                                fp.write(pdf)
                        except Exception as e:
                            print(f"      [WARN] no se guardo PDF: {e}")

                    fila_csv["pdf_sha1"] = hashlib.sha1(pdf).hexdigest()[:12]
                    fila_csv["pdf_menciona_codigo"] = codigo_en_pdf(
                        pdf, datos["codigo_sisin"])

                    loc, metodo, error, total_benef = extraer_localizacion(
                        pdf, datos["cod_inversion"], datos["codigo_sisin"])
                    fila_csv["metodo_extraccion"] = metodo

                    if loc:
                        fila_csv.update(
                            aplanar_localizacion(loc, total_benef))
                        mostrar_extraccion(
                            datos, url_pdf, metodo, loc, total_benef)
                    else:
                        fila_csv["error_pdf"] = error
                        print(f"\n      [!] SIN LOCALIZACION: {error}\n")

                except Exception as e:
                    fila_csv["error_pdf"] = str(e)[:250]
                    print(f"\n      [ERR] {e}\n")
                    if os.environ.get("SISIN_DEBUG_STACK"):
                        traceback.print_exc()

                finally:
                    if ventana_original is not None:
                        try:
                            cerrar_pestanas_extra(driver, ventana_original)
                        except Exception:
                            pass

                writer.writerow(fila_csv)
                f_csv.flush()
                try:
                    f_json.write(json.dumps(
                        {**datos, "url_pdf": url_pdf},
                        ensure_ascii=False) + "\n")
                    f_json.flush()
                except Exception as e:
                    print(f"      [WARN] JSONL: {e}")

                time.sleep(PAUSA_ENTRE_FILAS)

    except KeyboardInterrupt:
        print("\n[INFO] Interrumpido por el usuario.")
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n[ERR FATAL] {e}")
        traceback.print_exc()
    finally:
        try:
            f_csv.close()
        except Exception:
            pass
        try:
            f_json.close()
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()