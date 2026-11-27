# SPDX-License-Identifier: Apache-2.0
"""A small client for the Flagsmith Admin API: find the feature state that changed and turn it off.

Works against self-hosted Flagsmith (http://flagsmith:8000) and Flagsmith SaaS (https://api.flagsmith.com).
Authenticates with an organisation API key (`Authorization: Api-Key ...`)."""
from __future__ import annotations

import httpx

from ..config import settings


class FlagsmithError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.flagsmith_url and settings.flagsmith_api_key)


class Flagsmith:
    def __init__(self, url: str | None = None, api_key: str | None = None, timeout: float = 10) -> None:
        self.url = (url or settings.flagsmith_url).rstrip("/")
        key = api_key or settings.flagsmith_api_key
        if not self.url or not key:
            raise FlagsmithError("Flagsmith is not connected: set TRACEBASE_FLAGSMITH_URL and TRACEBASE_FLAGSMITH_API_KEY")
        self.http = httpx.Client(base_url=self.url + "/api/v1", timeout=timeout,
                                 headers={"Authorization": f"Api-Key {key}", "Accept": "application/json"})

    def _req(self, method: str, path: str, **kw):
        try:
            r = self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise FlagsmithError(f"cannot reach Flagsmith at {self.url}: {e}") from e
        if r.status_code >= 400:
            raise FlagsmithError(f"Flagsmith {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    @staticmethod
    def _results(data):
        return data.get("results", []) if isinstance(data, dict) else data

    def feature_states(self, environment_id: int, feature_id: int, **filters) -> list[dict]:
        params = {"environment": environment_id, "feature": feature_id, **filters}
        return self._results(self._req("GET", "/features/featurestates/", params=params))

    def find_state(self, environment_id: int, feature_id: int, segment_id: int | None = None) -> dict:
        """The environment default state of the feature, or its override for one segment."""
        filters = {}
        if segment_id is not None:
            fsegs = self._results(self._req("GET", "/features/feature-segments/",
                                            params={"environment": environment_id, "feature": feature_id}))
            fseg = next((f for f in fsegs if f.get("segment") == segment_id), None)
            if not fseg:
                raise FlagsmithError(f"feature {feature_id} has no override for segment {segment_id}")
            filters["feature_segment"] = fseg["id"]
        for s in self.feature_states(environment_id, feature_id, **filters):
            if s.get("identity") is None and (segment_id is not None or s.get("feature_segment") is None):
                return s
        where = f"segment {segment_id}" if segment_id is not None else f"environment {environment_id}"
        raise FlagsmithError(f"no feature state for feature {feature_id} in {where}")

    def set_enabled(self, state: dict, enabled: bool) -> dict:
        # environment and feature are sent along: Flagsmith's serializer needs the environment even on a partial update
        # (without it, PATCH fails with a 500, KeyError 'environment', as of Flagsmith 2.295)
        return self._req("PATCH", f"/features/featurestates/{state['id']}/",
                         json={"enabled": enabled, "environment": state["environment"], "feature": state["feature"]})
