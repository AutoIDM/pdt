# Example deployment

The output of a first deploy of the `hello-world-example` app to Azure, then `pdt logs`, `pdt health`, and `pdt run --deployed`. The [README](../README.md#example-deployment) shows the same run as an image.

```text
visch@ubuntu:~/pdt-examples$ pdt deploy hello-world-example
  hello-world-example
      runs run.py
      .env file read: .env
[alert] Deploying hello-world-example to Azure
Docker found, so the deploy will build the image with Docker on this computer.
Checking your Azure sign-in...
Checking current state in Azure subscription 3f7c321a-ccc7-41b0-9b6b-f5fd14bef70a (eastus2)...
Plan:
  register required Azure resource providers
  use existing resource group pdt-shared (shared by every pdt project in this subscription)
  use existing Log Analytics workspace pdt-logs in pdt-shared
  use existing Container Apps environment pdt-shared/pdt-eastus2 (shared by every pdt project in this subscription)
  use existing resource group pdt
  use existing ACR pdt232959dc77 (Basic)
  keep ACR authentication-as-arm on pdt232959dc77 (required for managed-identity image pulls)
  create lock pdt-hello-world-example on ACR pdt232959dc77 (keeps it while hello-world-example is deployed)
  create lock pdt-hello-world-example-in-pdt on Container Apps environment pdt-shared/pdt-eastus2 (keeps it while hello-world-example is deployed)
  use existing managed identity pdt-runner
  use existing Key Vault pdt-232959dc77 (RBAC)
  ensure scoped Key Vault secret permissions for the deployer and managed identity
  use existing storage account pdtdatacfa3bd4e8d, container pdt-data-cfa3bd4e8d (kept after destroy)
  grant pdt-hello-world-example-b4b1561 write access to hello-world-example/ in storage account pdtdatacfa3bd4e8d, container pdt-data-cfa3bd4e8d
  grant the signed-in Azure account write access to pdt-data-cfa3bd4e8d (for pdt storage)
  build and push image pdt232959dc77.azurecr.io/hello-world-example:latest with Docker on this computer
  create Container Apps Job pdt-hello-world-example-b4b1561: "0 0 * * *" (UTC)
Estimated monthly cost (eastus2 list prices, before free grants):
  Container Apps job: ~30 runs x 5 min assumed x 0.5 vCPU / 1 GiB  $      0.14
  Container Registry (Basic, shared)                               $      5.07
  storage: 0 objects (0.00 GB)                                     $      0.00
  total                                                            $USD   5.21
  excludes ACR storage and Log Analytics ingestion
Proceed? [y/N] y
==> reconciling resource group pdt-shared
==> reconciling resource group pdt
==> creating lock pdt-hello-world-example on ACR pdt232959dc77
==> creating lock pdt-hello-world-example-in-pdt on Container Apps environment pdt-shared/pdt-eastus2
==> building and pushing image pdt232959dc77.azurecr.io/hello-world-example:latest with Docker on this computer
[+] Building 6.8s (12/12) FINISHED                                           docker:default
 => [internal] load build definition from Dockerfile                                   0.0s
 => => transferring dockerfile: 721B                                                   0.0s
 => [internal] load metadata for ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256  0.0s
 => [internal] load .dockerignore                                                      0.0s
 => => transferring context: 2B                                                        0.0s
 => CACHED [1/7] FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bc  0.0s
 => => resolve ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595  0.0s
 => [internal] load build context                                                      0.0s
 => => transferring context: 115.45kB                                                  0.0s
 => [2/7] COPY .pdt-lifecycle /opt/pdt-lifecycle                                       0.0s
 => [3/7] RUN uv sync --script /opt/pdt-lifecycle/pdt/lifecycle.py                     0.9s
 => [4/7] WORKDIR /workspace/hello-world-example                                       0.0s
 => [5/7] COPY .pdt-script .                                                           0.0s
 => [6/7] RUN uv sync --script run.py                                                  2.6s
 => [7/7] COPY . /workspace                                                            0.1s
 => exporting to image                                                                 2.8s
 => => exporting layers                                                                1.6s
 => => exporting manifest sha256:f624ef6118df5202e94732f52d151264008454455cacf9414e53  0.0s
 => => exporting config sha256:b3297e1654f0b27e8c50490ee7d50e0b0faa695d1bf7ef672d884c  0.0s
 => => exporting attestation manifest sha256:0a0b3b0cb90ff92928e6871e5f347c8e217ecf17  0.0s
 => => exporting manifest list sha256:e7d7d4897901084d6c309368a332d2d1190220ed3fe2d47  0.0s
 => => naming to pdt232959dc77.azurecr.io/hello-world-example:latest                   0.0s
 => => unpacking to pdt232959dc77.azurecr.io/hello-world-example:latest                1.1s
The push refers to repository [pdt232959dc77.azurecr.io/hello-world-example]
f59651dbc0ea: Mounted from powershell-example
d0f0708d3bb8: Pushed
44136fa355b3: Mounted from powershell-example
4831516dd0cb: Mounted from powershell-example
322e9e97e47b: Mounted from powershell-example
15155ecba1d2: Mounted from powershell-example
60e9b7a2b36e: Pushed
590f2846d3a7: Mounted from powershell-example
7adbaef83469: Pushed
17c32a4f7dcc: Pushed
083692ad0723: Pushed
99bc6de3a71e: Pushed
a9cbac14dfc0: Pushed
latest: digest: sha256:e7d7d4897901084d6c309368a332d2d1190220ed3fe2d4766beb06878889821f size: 856
==> reconciling Container Apps Job pdt-hello-world-example-b4b1561
Deployed hello-world-example.
[alert] Deployed hello-world-example to Azure

Start a run now and show its log? [y/N] y
Starting a run of hello-world-example...
Started hello-world-example: execution pdt-hello-world-example-b4b1561-mgkps9k.
run 1 of hello-world-example: started 2026-10-21 22:18:17
watching for log output for hello-world-example; press Ctrl+C to stop, the run keeps going
22:18:33         [alert] Run of hello-world-example started
22:18:34 INFO    Hello, world!  app=hello-world-example
22:18:34         [alert] Run of hello-world-example completed
22:18:34         pdt: exit 0
the run succeeded after 26s, exit 0

Next steps:
  pdt logs hello-world-example    read the log of the newest run
  pdt runs hello-world-example    list the recent runs
  pdt health hello-world-example  show whether the last run succeeded

visch@ubuntu:~/pdt-examples$ pdt logs hello-world-example
run 1 of hello-world-example: started 2026-10-21 22:18:17, 26s, succeeded, exit 0
22:18:33         [alert] Run of hello-world-example started
22:18:34 INFO    Hello, world!  app=hello-world-example
22:18:34         [alert] Run of hello-world-example completed
22:18:34         pdt: exit 0

visch@ubuntu:~/pdt-examples$ pdt health hello-world-example
App                  Status  Last run             Recent
hello-world-example  ok      2026-10-21 22:18:17  1 of 1 succeeded

visch@ubuntu:~/pdt-examples$ pdt run hello-world-example --deployed
Started hello-world-example: execution pdt-hello-world-example-b4b1561-jov860r.
```
