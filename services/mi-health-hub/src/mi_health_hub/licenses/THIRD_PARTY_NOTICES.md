# Third-party notices

The root MIT license covers original project code. It does not replace the
licenses of third-party dependencies or the file-specific exception below.

| Component | Use | License / source |
|---|---|---|
| Chart.js 4.4.4 | Bundled chart renderer in the health web UI; original notice retained | [MIT](Chart.js-MIT.txt), [Chart.js](https://github.com/chartjs/Chart.js) |
| MiService / miservice_fork | Installed Xiaomi API dependency, not vendored | [MIT notice](MiService-MIT.txt), [MiService](https://github.com/Yonsm/MiService) |
| aiohttp | Installed asynchronous HTTP dependency | Apache-2.0 AND MIT, package distributions carry notices |
| paho-mqtt | Installed MQTT dependency | EPL-2.0 OR BSD-3-Clause |
| FastAPI, HTTPX, PyYAML, APScheduler, pytest | Installed health/runtime or test dependencies | See the license metadata shipped in each distribution |
| Open-Meteo / GeoNames | Optional weather and location data provider; no data bundled | Provider attribution remains in tool results; see the provider terms |

`services/xiaomi-ai-bridge/src/xiaomi_ai_bridge/kelvinator_ir.py` is distributed
under **LGPL-2.1-or-later** as a file-specific exception, to preserve attribution
to the IRremoteESP8266 protocol reference. The complete source is included and
the license text is in [licenses/LGPL-2.1.txt](LGPL-2.1.txt). Protocol
references: [IRremoteESP8266](https://github.com/crankyoldgit/IRremoteESP8266)
and [python-miio](https://github.com/rytilahti/python-miio). No C/C++ firmware,
python-miio package, captured user signal, or appliance credentials are bundled.

Android uses Android Framework APIs; the SDK/build tools are obtained separately
from Google. No SDK binaries, existing signing keys or signed private APKs are
redistributed. Xiaomi and other product names are used to identify compatible
services, not to imply endorsement.
