"""Publicador automático de Encuentro Espiritual (corre en GitHub Actions, sin Claude ni computadora prendida).

Cada vez que corre:
1. Busca en Notion (tabla Ideas Carrusel) las filas con Estado = Programado, "Aprobado por Iara" marcado
   y "Publicar el" ya cumplido (con un máximo de 24 horas de atraso).
2. Baja las láminas de la carpeta de Google Drive del carrusel (subcarpeta cuyo nombre contiene el número N).
3. Las sube a este repositorio (carpeta carruseles/NN) para que Instagram pueda leerlas por una dirección pública.
4. Publica el carrusel en Instagram con el texto de "Copy (leyenda)".
5. Marca la fila en Notion como Publicado y guarda el link.

Variables (secretos de GitHub): NOTION_TOKEN, IG_USER_ID, IG_TOKEN, GDRIVE_SA_JSON, DRIVE_FOLDER_ID.
Modo prueba: PRUEBA=1 hace todo menos publicar y marcar en Notion.
"""
import io, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

DB = "3f1592dec9a28041bbc7fe1842d0fdd9"          # Ideas Carrusel
GRAPH = "https://graph.facebook.com/v21.0"
REPO = os.environ.get("GITHUB_REPOSITORY", "iararubin6-boop/encuentro-publicaciones")
RAMA = os.environ.get("GITHUB_REF_NAME", "main")
PRUEBA = os.environ.get("PRUEBA") == "1"
AHORA = datetime.now(timezone.utc)

def log(*a):
    print(*a, flush=True)

def pedir(url, metodo="GET", datos=None, headers=None, form=False, intentos=4):
    for i in range(intentos):
        cuerpo = None
        if datos is not None:
            cuerpo = urllib.parse.urlencode(datos).encode() if form else json.dumps(datos).encode()
        req = urllib.request.Request(url, data=cuerpo, method=metodo, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{metodo} {url.split('?')[0]} → {e.code}: {e.read().decode()[:400]}")
        except (urllib.error.URLError, TimeoutError):
            if i == intentos - 1 or url.endswith("media_publish"):
                raise
            time.sleep(4)

# ---------- Notion ----------
def notion(ruta, metodo="GET", datos=None):
    return pedir(f"https://api.notion.com/v1/{ruta}", metodo, datos, {
        "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
        "Notion-Version": "2022-06-28", "Content-Type": "application/json"})

def texto(prop):
    return "".join(t.get("plain_text", "") for t in (prop or {}).get("rich_text", []) or (prop or {}).get("title", []))

def programados():
    q = notion(f"databases/{DB}/query", "POST", {"filter": {"and": [
        {"property": "Estado", "select": {"equals": "Programado"}},
        {"property": "Aprobado por Iara", "checkbox": {"equals": True}},
        {"property": "Publicar el", "date": {"is_not_empty": True}}]}})
    listos = []
    for p in q.get("results", []):
        pr = p["properties"]
        inicio = pr["Publicar el"]["date"]["start"]
        cuando = datetime.fromisoformat(inicio.replace("Z", "+00:00"))
        if cuando.tzinfo is None:  # fecha sin hora: se publica a las 12 UTC de ese día
            cuando = cuando.replace(hour=12, tzinfo=timezone.utc)
        if cuando > AHORA:
            continue
        if AHORA - cuando > timedelta(hours=24):
            log(f"Carrusel {pr['N']['number']}: atrasado más de 24 h, no se publica solo. Reprogramalo.")
            continue
        listos.append({"id": p["id"], "n": int(pr["N"]["number"]), "nombre": texto(pr["Nombre"]),
                       "copy": texto(pr.get("Copy (leyenda)"))})
    return listos

def marcar_publicado(page_id, link):
    notion(f"pages/{page_id}", "PATCH", {"properties": {
        "Estado": {"select": {"name": "Publicado"}},
        "Link publicado": {"url": link}}})

# ---------- Google Drive ----------
def drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(os.environ["GDRIVE_SA_JSON"])
    cred = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=cred, cache_discovery=False)

def bajar_laminas(n, destino):
    from googleapiclient.http import MediaIoBaseDownload
    svc = drive()
    raiz = os.environ["DRIVE_FOLDER_ID"]
    carpetas = svc.files().list(q=f"'{raiz}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                                fields="files(id,name)", pageSize=200, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()["files"]
    patron = re.compile(rf"(^|\D)0*{n}(\D|$)")
    elegidas = [c for c in carpetas if patron.search(c["name"])]
    if not elegidas:
        raise RuntimeError(f"no hay carpeta del carrusel {n} en Drive (nombre con el número {n}, por ejemplo «Carrusel {n:02d}»)")
    fotos = svc.files().list(q=f"'{elegidas[0]['id']}' in parents and mimeType contains 'image/' and trashed=false",
                             fields="files(id,name,mimeType)", pageSize=50, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()["files"]
    clave = lambda f: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", f["name"].lower())]
    fotos.sort(key=clave)
    if not fotos:
        raise RuntimeError(f"la carpeta «{elegidas[0]['name']}» está vacía")
    if len(fotos) > 20:
        raise RuntimeError(f"la carpeta tiene {len(fotos)} imágenes; Instagram acepta hasta 20")
    os.makedirs(destino, exist_ok=True)
    nombres = []
    for i, f in enumerate(fotos, 1):
        nombre = f"c{n:02d}-s{i:02d}.jpg"
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=f["id"], supportsAllDrives=True))
        hecho = False
        while not hecho:
            _, hecho = dl.next_chunk()
        datos = buf.getvalue()
        if f["mimeType"] != "image/jpeg":  # Instagram solo acepta JPG
            from PIL import Image
            img = Image.open(io.BytesIO(datos)).convert("RGB")
            out = io.BytesIO(); img.save(out, "JPEG", quality=92); datos = out.getvalue()
        open(os.path.join(destino, nombre), "wb").write(datos)
        nombres.append(nombre)
    log(f"Carrusel {n}: {len(nombres)} láminas bajadas de Drive («{elegidas[0]['name']}»).")
    return nombres

def subir_al_repo(n, carpeta):
    subprocess.run(["git", "add", carpeta], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "-c", "user.name=publicador", "-c", "user.email=publicador@users.noreply.github.com",
                    "commit", "-m", f"Carrusel {n:02d}: láminas para publicar"], check=True)
    subprocess.run(["git", "push"], check=True)

def esperar_publicas(urls):
    for u in urls:
        for _ in range(40):
            try:
                with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=20) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            time.sleep(5)
        else:
            raise RuntimeError(f"la imagen no quedó pública a tiempo: {u}")

# ---------- Instagram ----------
def ig_post(ruta, datos):
    datos = dict(datos, access_token=os.environ["IG_TOKEN"])
    return pedir(f"{GRAPH}/{ruta}", "POST", datos, form=True)

def ig_get(ruta, campos):
    return pedir(f"{GRAPH}/{ruta}?" + urllib.parse.urlencode({"fields": campos, "access_token": os.environ["IG_TOKEN"]}))

def esperar_contenedor(cid):
    for _ in range(60):
        estado = ig_get(cid, "status_code").get("status_code")
        if estado == "FINISHED":
            return
        if estado in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram rechazó el contenedor {cid}: {estado}")
        time.sleep(3)
    raise RuntimeError("Instagram tardó demasiado en procesar las láminas")

def publicar(urls, copy):
    ig = os.environ["IG_USER_ID"]
    hijos = []
    for u in urls:
        hijos.append(ig_post(f"{ig}/media", {"image_url": u, "is_carousel_item": "true"})["id"])
        log(f"  lámina {len(hijos)}/{len(urls)} cargada")
    for h in hijos:
        esperar_contenedor(h)
    cont = ig_post(f"{ig}/media", {"media_type": "CAROUSEL", "children": ",".join(hijos), "caption": copy})["id"]
    esperar_contenedor(cont)
    if PRUEBA:
        log("  PRUEBA: carrusel preparado, no se publica.")
        return None
    mid = ig_post(f"{ig}/media_publish", {"creation_id": cont})["id"]
    return ig_get(mid, "permalink").get("permalink")

def main():
    filas = programados()
    if not filas:
        log("Nada para publicar.")
        return
    errores = 0
    for f in filas:
        n = f["n"]
        log(f"Carrusel {n} · {f['nombre']}")
        try:
            if not f["copy"].strip():
                raise RuntimeError("falta el texto en «Copy (leyenda)»")
            carpeta = f"carruseles/{n:02d}"
            nombres = bajar_laminas(n, carpeta)
            subir_al_repo(n, carpeta)
            urls = [f"https://raw.githubusercontent.com/{REPO}/{RAMA}/{carpeta}/{x}" for x in nombres]
            esperar_publicas(urls)
            link = publicar(urls, f["copy"])
            if link:
                marcar_publicado(f["id"], link)
                log(f"  PUBLICADO: {link}")
        except Exception as e:
            errores += 1
            log(f"  NO SE PUBLICÓ: {e}")
    if errores:
        sys.exit(1)  # GitHub te avisa por correo cuando una corrida falla

if __name__ == "__main__":
    main()
