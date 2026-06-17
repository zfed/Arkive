"""
Client minimale per le API REST (v2) dello Storage Service di Archivematica.

Autore: Federica Zanardini
Università degli Studi di Milano - Direzione ICT
Data: 2026-06
Sviluppato con il supporto di Claude AI (Anthropic)

Riferimento ufficiale: https://wiki.archivematica.org/Storage_Service_API

Configurazione (variabili d'ambiente):
    SS_BASE_URL   es. http://localhost:8000
    SS_USERNAME   utente API dello Storage Service
    SS_API_KEY    API key dell'utente (Storage Service > Administration > Users)
"""
import os

import requests


class StorageServiceError(Exception):
    pass


class StorageServiceClient:
    def __init__(self, base_url=None, username=None, api_key=None, timeout=30):
        self.base_url = (base_url or os.environ.get("SS_BASE_URL", "")).rstrip("/")
        self.username = username or os.environ.get("SS_USERNAME", "")
        self.api_key = api_key or os.environ.get("SS_API_KEY", "")
        self.timeout = timeout

        if not all([self.base_url, self.username, self.api_key]):
            raise StorageServiceError(
                "Configurazione mancante: imposta SS_BASE_URL, SS_USERNAME e SS_API_KEY "
                "come variabili d'ambiente (vedi .env.example)."
            )

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"ApiKey {self.username}:{self.api_key}",
            "Accept": "application/json",
        })

    def _url(self, path):
        return f"{self.base_url}{path}"

    def list_packages(self, package_type="AIP", extra_filters=None, page_size=50):
        """Generatore che restituisce tutti i package che soddisfano i filtri,
        gestendo la paginazione dell'API."""
        offset = 0
        params = {"package_type": package_type, "limit": page_size}
        if extra_filters:
            params.update(extra_filters)

        while True:
            params["offset"] = offset
            resp = self.session.get(self._url("/api/v2/file/"), params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            objects = data.get("objects", [])
            for obj in objects:
                yield obj

            meta = data.get("meta", {})
            if not meta.get("next"):
                break
            offset += page_size

    def get_package(self, uuid):
        resp = self.session.get(self._url(f"/api/v2/file/{uuid}/"), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def request_deletion(self, uuid, event_reason, pipeline_uuid, user_id, user_email):
        """
        Invia una RICHIESTA di cancellazione per l'AIP indicato.

        Importante: questa chiamata NON cancella l'AIP. Crea una richiesta
        che deve essere approvata da un amministratore tramite l'interfaccia
        web dello Storage Service (Packages). E' un controllo di sicurezza
        intenzionale di Archivematica per evitare cancellazioni accidentali
        o non supervisionate, e non esiste un endpoint pubblico documentato
        per automatizzare anche l'approvazione.
        """
        payload = {
            "event_reason": event_reason,
            "pipeline": pipeline_uuid,
            "user_id": int(user_id),
            "user_email": user_email,
        }
        resp = self.session.post(
            self._url(f"/api/v2/file/{uuid}/delete_aip/"),
            json=payload,
            timeout=self.timeout,
        )
        return resp
