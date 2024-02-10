import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import click
import requests
import structlog
import uvicorn
import yaml
from fastapi import FastAPI, Request, Response

log = structlog.get_logger()
app = FastAPI()
sc: "SchemaCache"

HOME = str(Path.home())


@dataclass(frozen=True)
class Auth:
    ca_cert: str | None
    client_cert: str | None
    client_key: str | None
    token: str | None


class SchemaCache:
    def __init__(self, server: str, auth: Auth):
        self._lock = Lock()

        self._server = server
        self._auth = auth
        self._schemas: dict[str, Any] = {}

    @contextmanager
    def _get_session(self):
        try:
            s = requests.Session()
            if self._auth.ca_cert:
                s.verify = self._auth.ca_cert
            if self._auth.client_cert and self._auth.client_key:
                s.cert = (self._auth.client_cert, self._auth.client_key)
            if self._auth.token:
                s.headers.update({"Authorization": f"Bearer {self._auth.token}"})
            yield s
        finally:
            pass

    def _cleanup_schema(self, v: dict[str, Any]):
        # Don't include a default option
        v.pop("default", "")

        # Flatten any allOf $refs
        if (
            "allOf" in v
            and isinstance(v["allOf"], list)
            and len(v["allOf"]) == 1
            and len(v["allOf"][0]) == 1
        ):
            v["$ref"] = v.pop("allOf")[0]["$ref"]

        # Reqrite $refs to point to defintions
        if "$ref" in v and isinstance(v["$ref"], str):
            v["$ref"] = v["$ref"].replace("#/components/schemas/", "#/definitions/")

        if "enum" in v and isinstance(v["enum"], list):
            v["enum"] = list(set(v["enum"]))

        for k, v1 in v.items():
            if isinstance(v1, dict):
                self._cleanup_schema(v1)
            elif isinstance(v1, list):
                for v2 in v1:
                    if isinstance(v2, dict):
                        self._cleanup_schema(v2)

    def _fix_kind(self, v: dict[str, Any]):
        if "x-kubernetes-group-version-kind" in v:
            if "kind" in v["properties"]:
                v["properties"]["kind"]["enum"] = []
                for k in v["x-kubernetes-group-version-kind"]:
                    if k["kind"] not in v["properties"]["kind"]["enum"]:
                        v["properties"]["kind"]["enum"].append(k["kind"])

            if "apiVersion" in v["properties"]:
                v["properties"]["apiVersion"]["enum"] = []
                for k in v["x-kubernetes-group-version-kind"]:
                    if k["group"] not in v["properties"]["apiVersion"]["enum"]:
                        if len(k["group"]) > 0:
                            apiVersion = f"{k['group']}/{k['version']}"
                        else:
                            apiVersion = k["version"]
                        v["properties"]["apiVersion"]["enum"].append(apiVersion)

    def update(self):
        with self._lock:
            self._schemas = {}

            with self._get_session() as s:
                log.info("Fetching OpenAPIv3 Spec")
                resp = s.get(f"{self._server}/openapi/v3")
                if resp.status_code != 200:
                    log.error("Failed to get schema", status_code=resp.status_code)
                    return

                paths = list(resp.json().get("paths", {}).keys())
                for path in paths:
                    log.debug("Fetching schema", path=f"openapi/v3/{path}")
                    resp = s.get(f"{self._server}/openapi/v3/{path}")

                    if resp.status_code != 200:
                        log.error(
                            "Failed to get schema",
                            path=path,
                            status_code=resp.status_code,
                        )
                        continue

                    self._schemas.update(
                        resp.json().get("components", {}).get("schemas", {})
                    )

                log.info("Got schemas", count=len(self._schemas))
                for v in self._schemas.values():
                    if isinstance(v, dict):
                        self._fix_kind(v)
                        if "x-kubernetes-preserve-unknown-fields" not in v:
                            v["additionalProperties"] = False
                        self._cleanup_schema(v)

    @property
    def paths(self):
        with self._lock:
            return list(self._schemas.keys())

    @property
    def schemas(self):
        with self._lock:
            return self._schemas


@app.get("/all.json")
def all():
    paths = []
    for path in sc.paths:
        paths.append({"$ref": f"_definitions.json#definitions/{path}"})
    return {"oneOf": paths}


@app.get("/_definitions.json")
def schemas():
    return {"definitions": sc.schemas}


@app.middleware("http")
async def logging_middleware(request: Request, call_next) -> Response:
    response = Response(status_code=500)
    try:
        response = await call_next(request)
    except Exception as err:
        log.error("Uncaught exception", err=err)
        raise
    finally:
        log.info(
            "HTTP request",
            url=str(request.url),
            status_code=response.status_code,
            method=request.method,
            client_ip=request.client.host,
            client_port=request.client.port,
        )
        return response


@click.command()
@click.option("-h", "--host", default="0.0.0.0", help="Port for HTTP API to listen")
@click.option("-p", "--port", default=8000, help="Address for HTTP API to listen")
@click.option(
    "-k",
    "--kube-file",
    default=f"{HOME}/.kube/config",
    type=click.Path(resolve_path=True, file_okay=True, dir_okay=False, readable=True),
    help="Kube Config file to read context from",
)
@click.option(
    "-i",
    "--interval",
    default=3600,
    help="Interval to update schemas from K8s (seconds)",
)
def main(host: str, port: int, kube_file: str, interval: int):
    global sc

    try:
        with open(kube_file) as f:
            kube = yaml.safe_load(f.read())

            contexts = {c["name"]: c["context"] for c in kube["contexts"]}
            clusters = {c["name"]: c["cluster"] for c in kube["clusters"]}
            users = {u["name"]: u["user"] for u in kube["users"]}

            ctx = kube["current-context"]
            userName = contexts[ctx]["user"]
            clusterName = contexts[ctx]["cluster"]

            user = users[userName]
            cluster = clusters[clusterName]

            server = cluster["server"]
            ca = cluster.get("certificate-authority")
            client_key = user.get("client-key")
            client_cert = user.get("client-certificate")
            token = user.get("token")

            auth = Auth(ca, client_cert, client_key, token)
    except KeyError as e:
        log.error("Bad KubeConfig", err=e)
        return

    running = Event()
    sc = SchemaCache(server, auth)

    def update_cache():
        last_run = 0.0
        while running.is_set():
            if time.time() > last_run + interval:
                sc.update()
                last_run = time.time()
            time.sleep(0.1)

    t = Thread(target=update_cache)
    t.daemon = True

    running.set()
    t.start()
    log.info("Starting schema server", host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_config=None)
    running.clear()
    t.join()
