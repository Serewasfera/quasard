"""Имитатор первой настройки станции: повторяет HTTP-запросы Яндекс Станции 2 при активации и старте.

    python tools/activation_sim.py --base https://127.0.0.1:8443
    python tools/activation_sim.py --base https://127.0.0.1 --verify   # с проверкой цепочки сертификата

Хост подставляется заголовком Host (как при подмене DNS), поэтому сервер видит те же запросы, что от станции.
"""
from __future__ import annotations

import argparse
import json
import time

import httpx

DEVICE_ID = "SIMSTATION0001"
UA = "yandexstation_2/1.56.1.34.1278844160.20220415.22"
FW = "1.56.1.34.1278844160.20220415.22"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8443")
    ap.add_argument("--verify", action="store_true", help="проверять сертификат (имя хоста берётся из Host)")
    args = ap.parse_args()
    ok = True

    def call(method: str, host: str, path: str, **kw):
        nonlocal ok
        headers = {"Host": host, "User-Agent": UA, **kw.pop("headers", {})}
        ext = {"sni_hostname": host} if args.verify else {}
        t0 = time.monotonic()
        r = client.request(method, args.base + path, headers=headers, extensions=ext, **kw)
        body = r.text
        good = r.status_code in (200, 204)
        ok &= good
        print(f"{'OK ' if good else 'ERR'} {r.status_code} {method} {host}{path.split('?')[0]} "
              f"{(time.monotonic() - t0) * 1000:.0f}ms  {body[:110]!r}")
        return r

    with httpx.Client(verify=args.verify, timeout=15) as client:
        tok = call("POST", "oauth.yandex.ru", "/token", data={
            "grant_type": "authorization_code", "code": "abcd1234", "client_id": "x", "client_secret": "y",
            "device_id": DEVICE_ID, "device_name": "Яндекс.Станция 2"}).json()["access_token"]
        tok = call("POST", "oauth.yandex.ru", "/token", data={
            "grant_type": "x-token", "access_token": tok, "client_id": "x", "client_secret": "y",
            "device_id": DEVICE_ID}).json()["access_token"]
        auth = {"Authorization": f"OAuth {tok}"}
        q = f"device_id={DEVICE_ID}&platform=yandexstation_2&revision=rev3"
        call("GET", "startup.mobile.yandex.net", f"/analytics/startup?deviceid={DEVICE_ID}&api_key_128=k")
        call("POST", "quasar.yandex.net", f"/register?{q}&name=quasar&activation_code=123&firmware_version={FW}"
             f"&ts={int(time.time())}", headers={**auth, "X-Quasar-Signature": "sig", "X-Quasar-Signature-Version": "2"},
             content=b"{}")
        call("GET", "quasar.yandex.net", f"/check_updates?{q}&version={FW}")
        call("POST", "quasar.yandex.net", f"/push_subscribe?{q}&uuid=1&service=quasar-realtime", headers=auth)
        call("GET", "quasar.yandex.net", "/glagol/device_list", headers=auth)
        info = call("GET", "quasar.yandex.net", f"/get_sync_info?{q}", headers=auth).json()
        call("GET", "quasar.yandex.net", "/get_timezone?lat=55.7&lon=37.6")
        call("GET", "quasar.yandex.net", "/get_user_details", headers=auth)
        call("POST", "report.appmetrica.yandex.net", f"/report?deviceid={DEVICE_ID}", content=b"x")
        call("POST", "clck.yandex.net", "/quasar_metrics/write/json/batch", content=b"x")
        call("GET", "scbh.yandex.net", "/generate_204")
        sysc = info.get("config", {}).get("system_config", {})
        print("\nget_sync_info: uniProxyUrl =", sysc.get("uniProxyUrl"),
              "| useProtoAliceApi =", sysc.get("voiceDialogSettings", {}).get("useProtoAliceApi"))
    print("\nИТОГ:", "все запросы прошли" if ok else "есть ошибки")


if __name__ == "__main__":
    main()
