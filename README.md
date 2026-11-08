# POI Downloader (OpenStreetMap / Overpass)

Mengunduh Point of Interest (Node, Way, Relation) dari OpenStreetMap di dalam **batas administrasi provinsi** di Indonesia. Batas dicari lewat Nominatim, POI diambil lewat Overpass API (filter `area`, dengan fallback filter `poly` dari geometri boundary). Output: CSV dan/atau GeoJSON.

## Instalasi

```bash
pip install -r requirements.txt
```
Membutuhkan Python 3.8+ dan koneksi internet.

## Struktur

```
download_poi.py   # program utama (CLI + fungsi modular)
config.py         # mapping kategori, endpoint API, timeout, retry
output/           # hasil download
logs/             # poi_downloader.log
.cache/           # cache boundary provinsi
```

## Menjalankan

```bash
python download_poi.py --province "Jawa Barat" --category airport
```

| Parameter | Keterangan |
|---|---|
| `--province` | Nama provinsi (wajib), mis. `"Jawa Barat"`, `"DKI Jakarta"`, `Bali` |
| `--country` | Negara, default `Indonesia` |
| `--category` | Satu/lebih kategori dari mapping |
| `--tag` | Satu/lebih tag OSM `key=value` |
| `--output-dir` | Folder output (default `output`) |
| `--format` | `csv`, `geojson`, atau `both` (default) |
| `--overpass-url` | Ganti server Overpass |
| `-v` | Log detail (termasuk query) |

Minimal salah satu dari `--category` / `--tag` wajib diisi. Setiap kategori/tag disimpan ke file terpisah: `<provinsi>_<kategori>.csv|geojson` (mis. `jawa_barat_airport.csv`).

### Kategori
Tersedia: `airport, hospital, school, university, restaurant, hotel, bank, fuel, pharmacy, shopping_mall`.
Tambah kategori baru di `CATEGORY_MAPPING` pada `config.py`, mis. `"cafe": ["amenity=cafe"]`. Satu kategori boleh punya beberapa tag (digabung/union).
Kategori yang tidak dikenal akan menampilkan pesan agar memakai `--tag`.

### Custom tag
```bash
python download_poi.py --province "Jawa Barat" --tag amenity=school shop=supermarket
python download_poi.py --province "Bali" --category hotel --tag amenity=cafe
```
File untuk tag custom diberi nama `<provinsi>_<key>_<value>` (mis. `jawa_barat_amenity_school.csv`).

## Kolom output
`osm_id, osm_type, name, category, osm_tags (JSON), province, country, latitude, longitude, source, osm_url, download_date`.
Node memakai koordinat asli; Way/Relation memakai `center` dari Overpass. Data tanpa nama tetap disimpan. Duplikasi dihapus berdasarkan `(osm_type, osm_id)`. GeoJSON berisi geometri `Point` `[longitude, latitude]`.

## Contoh per provinsi

```bash
python download_poi.py --province "Jawa Barat"  --category airport hospital
python download_poi.py --province "Jawa Timur"  --category university bank --format csv
python download_poi.py --province "Jawa Tengah" --category hotel restaurant --format geojson
python download_poi.py --province "DKI Jakarta" --category shopping_mall fuel
python download_poi.py --province "Bali"        --category pharmacy --tag tourism=attraction
```

## Mengatasi error API

- **HTTP 429/502/503/504 atau timeout**: server Overpass sibuk. Script mencoba ulang otomatis (3x, backoff) dan beralih ke endpoint cadangan di `OVERPASS_ENDPOINTS`. Coba lagi beberapa menit kemudian, atau pakai `--overpass-url https://overpass.private.coffee/api/interpreter`.
- **Timeout untuk kategori besar** (mis. `restaurant`, `school`): naikkan `OVERPASS_QUERY_TIMEOUT` / `OVERPASS_HTTP_TIMEOUT` di `config.py`.
- **Boundary tidak ditemukan / salah**: periksa ejaan; pesan error menampilkan kandidat dari Nominatim. Hapus `.cache/` jika cache perlu dibuat ulang.
- **Hasil kosong**: tidak ada file ditulis. Periksa tag di <https://taginfo.openstreetmap.org>.
- **Area query kosong**: script otomatis mencoba fallback dengan polygon boundary.
- Semua proses dan error tercatat di `logs/poi_downloader.log`.

Data © OpenStreetMap contributors (ODbL). Patuhi kebijakan penggunaan Nominatim (maks. 1 request/detik) dan Overpass.

## Web App (POI Explorer)

Frontend Leaflet (static/poi_map.html) + backend FastAPI (app.py) yang memanggil pipeline downloader yang sama (download_poi.run_download).

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```n
Buka http://127.0.0.1:8000. Isi provinsi dan kategori/tag (pisahkan koma, mis. `airport, hospital, amenity=clinic`). Progres tampil real-time lewat SSE; CSV/GeoJSON disimpan ke `output/` dan tombol download di peta mengunduh file yang sama persis.

- `POST /api/poi` body: `{province, category[], tags[], format, stream}`. `stream=true` mengembalikan SSE, selain itu JSON berisi `geojson`, `files`, `datasets`.
- Respons juga berisi `csv` (teks) dan `geojson`; tombol download di frontend membuat file dari respons, tanpa bergantung pada file di server. `GET /api/health` untuk cek status.
- Jika beberapa kategori dipilih, selain file per kategori dibuat file gabungan (`<provinsi>_<kat1>_<kat2>`) tanpa duplikasi.
- Tes offline (mock): `python -m unittest tests.test_offline -v`; tes live: `python tests/e2e_live.py`.


## Deploy ke Vercel

1. Push project ke GitHub (`output/`, `logs/`, `.cache/` sudah di-ignore).
2. Vercel Dashboard -> Add New -> Project -> import repo. Framework preset: FastAPI/Other, tanpa build command; root = folder project ini.
3. Deploy. `vercel.json` sudah mengatur `app.py` dengan `maxDuration` 300 detik dan menyertakan `static/**`.
4. Buka domain `*.vercel.app`; `/` menampilkan POI Explorer, API di `/api/poi`.

Catatan: filesystem Vercel hanya-baca kecuali `/tmp` (tidak permanen); output/log/cache otomatis ke `/tmp` dan hasil dikirim langsung ke browser. `maxDuration` 300 detik dibatasi paket (Hobby maks 300 dengan Fluid Compute; Pro lebih tinggi, sesuaikan di `vercel.json`). Untuk provinsi/kategori besar, batas waktu bisa tercapai; pilih sedikit kategori per pencarian. Server Overpass publik dapat membatasi IP datacenter bersama; ganti endpoint di `config.py` jika perlu.

