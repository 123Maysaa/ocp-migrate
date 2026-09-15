# Migrasi konfigurasi OpenShift ke Vault

Pipeline membaca `env.yaml` dan `migrate.yaml`, menggabungkan konfigurasi ke Vault,
memverifikasi Secret hasil Vault Secrets Operator (VSO), kemudian **membuat Deployment
baru dengan satu replica**. Deployment/DC, Secret, ConfigMap, Service, dan Route sumber
tidak diubah atau dihapus. Folder `newvault01` hanya menjadi referensi desain.

## Menjalankan di Jenkins

1. Simpan seluruh folder ini dalam repository yang digunakan job Jenkins.
2. Gunakan Pipeline from SCM dengan Script Path **`pipeline.groovy`**.
3. Agent Linux/Unix membutuhkan `python3` (3.8+), `oc`, dan `vault` dalam PATH.
   Python memakai standard library; tidak membutuhkan PyYAML atau instalasi pip.
4. Plugin yang diperlukan: OpenShift Client, HashiCorp Vault (binding
   `VaultTokenCredentialBinding`), Credentials Binding, dan Pipeline Utility Steps
   (`readYaml`, `readJSON`, `writeJSON`).
5. Cluster configuration dan credential harus sesuai `env.yaml`. Semua akses OCP
   menggunakan konteks OpenShift Client Plugin, bukan asumsi bahwa shell sudah login.
6. Namespace dan CRD VSO harus tersedia. Credential OCP memerlukan akses baca sumber,
   create Deployment, serta get/create/patch resource VSO dan Secret holder.
   Credential Vault membutuhkan pengelolaan auth mount, KV mount/data, policy, dan AppRole.

```yaml
ocp: "crc-sip"
vaultaddr: "http://192.168.2.12:8200"
vaultcred: "sip-vault"
```

Parameter job: `ENV_FILE` (default `env.yaml`), `MIGRATE_FILE` (default `migrate.yaml`),
dan `SYNC_TIMEOUT_SECONDS` (default 180, per workload). `migrate.json` tidak digunakan.

## Pemetaan resource

Untuk namespace `task-api-a` dan sumber `pikachu-dc`:

| Komponen | Nama/path |
|---|---|
| Deployment baru | `new-pikachu-dc` |
| Auth mount | `task-api-a-approle` |
| AppRole | `pikachu-dc` |
| Endpoint role | `auth/task-api-a-approle/role/pikachu-dc` |
| KV v2 mount | `task-api-a-kv` |
| Path dalam mount | `pikachu-dc` |
| Policy read | `task-api-a-pikachu-dc-access` |
| VaultConnection | `vault-connection-task-api-a` |
| Secret holder | `holder-secret-pikachu-dc` |
| VaultAuth | `vaultauth-pikachu-dc` |
| VaultStaticSecret | `vaultstaticsecret-task-api-a-pikachu-dc` |
| Secret hasil VSO | `vaultsecret-task-api-a-pikachu-dc` |

Semua container yang terdaftar untuk satu workload berbagi satu path dan satu VSS.
Nama Vault mengikuti sumber; hanya nama Deployment baru dan value label workload/pod
yang diberi `new-`. Selector Deployment baru memakai label pod baru. Label namespace,
node, Service, Route, dan resource sumber tidak diprefix.

## Alur dan perilaku

1. Validasi input dan baca snapshot workload serta seluruh Secret/ConfigMap pilihan.
2. Decode seluruh key/value Secret, termasuk key yang tidak terpakai. Gabungkan dengan
   seluruh data ConfigMap dan env literal pilihan. Nama env yang tidak tercantum tetap
   mengikuti sumber. Input mengasumsikan tidak ada key dengan nilai berbenturan.
3. Buat manifest lokal. Lakukan server dry-run untuk Deployment dan resource VSO awal
   sebelum menulis Vault. Nama Deployment tujuan yang sudah ada menggagalkan run;
   pipeline memakai `create`, bukan overwrite Deployment existing.
4. Provision mount, policy read pada path data workload, AppRole, dan data KV v2.
   Buat SecretID baru lalu apply VaultConnection, holder, VaultAuth, dan VSS.
5. VSS menyinkronkan setiap 5 detik. `excludeRaw: true` meniadakan metadata `_raw`
   tambahan. Bandingkan persis key dan byte nilai sumber dengan Secret hasil VSO,
   termasuk newline. Semua workload harus lolos sebelum tahap create Deployment.
6. Buat Deployment baru dengan replica 1. Tidak menunggu rollout aplikasi atau
   mengubah traffic; pengujian dan scaling dilakukan manual.

`env.value` pilihan menjadi `env.valueFrom.secretKeyRef`. Referensi key Secret/ConfigMap
pilihan juga diarahkan ke Secret hasil VSO. **`envFrom` tetap `envFrom`**, mempertahankan
prefix dan urutan referensinya. Karena sumber digabung, `envFrom` membaca seluruh key
Secret gabungan, termasuk key dari sumber/container lain; bukan hanya subset asalnya.
Konsekuensi ini perlu diperiksa saat testing aplikasi. Referensi yang tidak dipilih tetap lokal.

Volume Secret/ConfigMap dan projected volume dipindahkan ke Secret hasil VSO dengan
daftar `items` dari sumber asli. Path file, mode, `subPath`, dan volumeMount dipertahankan.
Volume bersama harus dipilih oleh semua container pemakainya supaya perpindahan tidak
mengubah konfigurasi container di luar daftar migrasi. Init container dapat dicantumkan
dalam daftar `containers` menggunakan nama aslinya.

Konversi DC memakai pod template dan image yang sudah terselesaikan pada sumber.
Strategi Rolling/Recreate dipetakan ke Deployment; trigger ImageChange DC tidak disalin.
Deployment biasa mempertahankan strategi, probes, resources, service account, dan pod spec.

## Batas versi awal

- Tidak ada PVC sesuai asumsi scope.
- Tidak ada resolusi konflik key. Urutan penggabungan tidak boleh dipakai sebagai kontrak
  precedence jika ada konflik; kasus tersebut menjadi enhancement berikutnya.
- Env pilihan harus literal, tanpa ekspansi `$(...)`. Downward API/resourceFieldRef
  yang tidak dipilih tetap utuh. Untuk secretKeyRef/configMapKeyRef, cantumkan sumbernya
  di daftar Secret/ConfigMap, bukan env literal.
- Nilai harus UTF-8, termasuk ConfigMap binaryData. Data non-UTF8 ditolak sebelum
  provisioning karena membutuhkan transformasi biner khusus.
- DC Custom strategy atau lifecycle hooks ditolak agar tidak hilang diam-diam.
- Label yang menjadi lebih panjang dari 63 karakter setelah prefix ditolak.
  Affinity/topology/node selector existing dipertahankan; bukan seluruh selector label
  di pod yang diarahkan ke label workload baru.
- Tidak membuat Service/Route baru, tidak mengubah selector Service existing.
- Run tidak transaksional: kegagalan setelah provisioning dapat meninggalkan resource
  Vault/VSO; kegagalan pada tahap create dapat meninggalkan sebagian clone yang sudah
  dibuat. Tidak ada rollback/delete otomatis. Run ulang berhenti jika clone sudah ada.
- Mount existing dipakai hanya jika tipenya benar. Path KV dan policy/role dengan nama
  yang sama diselaraskan. `vault kv put` mengganti isi path dan menambah versi; SecretID
  dibuat baru setiap run tanpa pencabutan ID sebelumnya, mengikuti desain referensi.
- `skipTLSVerify: true` mengikuti template `newvault01`; alamat HTTP mengikuti env.yaml.

Snapshot/payload/credential sementara berada dalam `.migration-work` dengan direktori
mode 700, tanpa stash/archive, dan dihapus melalui `post always`. Jangan aktifkan verbose
plugin atau mengarsipkan workspace sensitif. Log verifikasi hanya mencetak nama key,
bukan nilainya. Output pembacaan Secret melewati runtime Jenkins, sehingga akses build
dan workspace tetap perlu dibatasi. Jika agent hilang, cleanup perlu dilakukan pada agent.

## Pemeriksaan lokal

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests memakai fixture dan mock Vault; tidak menghubungi cluster atau Vault sebenarnya.
Validasi runtime Jenkins, CRD/admission cluster, dan aplikasi dilakukan pada lingkungan
Jenkins/OCP. Referensi API: [VSO API](https://docs.hashicorp.com/vault/docs/deploy/kubernetes/vso/api-reference)
dan [OpenShift Client Plugin](https://github.com/jenkinsci/openshift-client-plugin).
