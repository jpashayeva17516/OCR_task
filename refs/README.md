# Reference data

Copy the example files to the corresponding `.json` names and fill them with authoritative data.

- `esr.json`: ESR station code to `[station_name, country_iso_alpha2]`, for example `{"547302": ["Station name", "TM"]}`.
- `gng_az.json`: GNG code to Azerbaijani cargo description, for example `{"73051100": "Azərbaycanca yük adı"}`.
- `parties.json`: normalized OCR party name to canonical company name and address, for example `{"djiar logistika terminallari": "ООО ..."}`.

The real JSON files are ignored by Git because they may contain private business data. Set `SMGS_REFS_DIR` if the references are stored outside the project.
