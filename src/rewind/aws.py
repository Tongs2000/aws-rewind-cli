"""Lazy, region-pinned boto3 client factory.

Operations receive this object instead of raw clients, so tests can substitute a fake
without patching boto3, and so the target region always comes from the plan or the
command line rather than from ambient state.
"""

from __future__ import annotations

from typing import Any, Dict


class AwsClients:
    def __init__(self, region: str, session: Any = None) -> None:
        self.region = region
        self._session = session
        self._clients: Dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            if self._session is None:
                import boto3

                self._session = boto3.session.Session()
            self._clients[service] = self._session.client(service, region_name=self.region)
        return self._clients[service]
