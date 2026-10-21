DISABLED EXPERIMENTAL CAMERA FAST PATH — image benchmark still pending

9 October 2026 update
The bundle job and referenced macro were registered through ZEN Job Mode.
A stationary inventory-only run collected Axiocam 305 settings at 200 ms,
CameraGamma=1 and the expected fixed WhiteBalance. It acquired no images and
changed no camera, illumination, focus or stage settings. ZEN then rejected
finalization because the inventory-only workflow had no result items:
  System.ArgumentException: itemsToBeFinalized must not be empty
The client preserved the ABORTED result and released its hardware lease.
Evidence: transport/c6de09c7990c48b783527470afda19c6 and
research/camera-latency-audit-02/HANDOFF.txt.
EasyHDR=true coexists with AxioCamHDR=false/AxioCamNativeHDR=false in the
inventory; no auto-white-balance readback binding has been established.
Do not remove or guess profile gates. No faster acquisition backend has been
qualified or enabled. The user has put acquisition on hold and is moving the
chips; any later motion requires fresh placement verification.

The completed overnight acquisition used the existing proven backend. Nothing
in this folder is imported by that backend. The implementation was built and
unit-tested offline before the inventory run above. profile.example.json defaults to enabled=false;
its unknown runtime parameter names are deliberately null.

What is implemented

One registered macro job stays alive while a filesystem client requests up to
12 stationary snapshots. Each request calls the installed, documented
Zen.Acquisition.AcquireImage() method once. The returned ZenImage is saved as
<request_uuid>.czi using Zen.Application.Save. The exact image/file relation
is explicit; no claim is made that a request UUID appears in a gRPC frame.
After client acknowledgement the image is closed. Job finalization happens
once at the end rather than after every snapshot. Snap time, profile-check
time, CZI save time and optional TIFF-export time are recorded separately.

Camera settings are VERIFIED, NEVER CHANGED. Begin a future stationary trial
after a normal proven-backend exposure establishes the intended fixed camera
profile. The prototype does not apply guessed camera properties or resource
values. It refuses an active live/continuous stream and any missing/mismatched
readback gate. It contains no stage movement, autofocus, objective, illumination,
AutoExposure, automatic white balance, dialog or burned-in annotation call.

Build output (offline, already generated)

  .venv\Scripts\python.exe scripts\camera_fastpath\build_bundle.py

  bundle\FlakeFinder Request Snap Prototype v1.czjob
  bundle\Macros\FlakeFinder Request Snap Prototype v1.czmac

These files derive their job/macro packaging from the installed official demo:
  C:\Users\Public\Documents\Carl Zeiss\ZENcore\Documents\Macro Jobs\Demo - Snap and save user defined number of images
The script uses only the installed ZenAcquisitionLM, ZenCamera and application
Save methods recorded in research/camera-latency-audit-01/installed-methods.json.
The generated job has ONLY a Macro task. It has no bootstrap camera task and
will not take an image merely to inventory settings. Registration and macro
resolution were demonstrated by the inventory run above. Other installations
must import both job and macro before running the client.

Future between-chip sequence — not executed

1. Finish/checkpoint the current acquisition and leave the microscope at one
   known stationary, focused position. Capture a baseline with the proven
   backend, preserving its TIFF+JSON and current camera profile. Do not use
   an old field at another XYZ as a pixel comparison.
2. Register the generated new job and macro, then return to supervised API
   Mode. The client uses the same HardwareLease as production and refuses
   another running/loaded job. It never force-unloads another job.
3. Read camera inventory without acquiring an image:
     .venv\Scripts\python.exe scripts\camera_fastpath\benchmark.py inventory --execute
   Without --execute this is a dry run and opens no connection.
4. Copy profile.example.json to a trial profile. Fill camera_name and the
   four parameter_name bindings ONLY from the returned inventory.json and
   verified camera semantics. Expected values are the existing fixed profile:
   200 ms; the saved white-balance coefficients; auto exposure, auto white
   balance and HDR false. If any role has no readable parameter, STOP HERE:
   that runtime binding is unresolved. Do not invent a name or remove the gate.
   Set enabled=true only for the intended stationary trial.
5. Run a small canary with same-position baseline:
     .venv\Scripts\python.exe scripts\camera_fastpath\benchmark.py benchmark --execute --profile <trial.json> --count 3 --baseline <baseline.tiff>
   This saves request-correlated CZI files, an optional first-image TIFF,
   profile readbacks and timing summaries under transport/<session_uuid>.
   TIFF export must have expected dimensions, uint16 dtype and ADC range.
   Its color/pixel equivalence is NOT automatically accepted: inspect the
   stationary image and recorded median RGB ratios, and verify no export
   scaling or loss before considering this a scientific acquisition backend.
   Raw CZI is retained even if export qualification fails. No CZI parser is
   installed in this environment; the client checks signature/size/hash only.

Cancellation and correlation contract

active.json contains a new session UUID and a deadline at most 120 seconds
away. The macro only accepts files within its fixed transport directory.
request-NNNN.json is immutable and contains session_id, request_id, sequence,
deadline_unix_s. One request is outstanding. reply-NNNN.json repeats these
identities, gives the request-named CZI path and separate timings/readbacks.
The client hashes the completed CZI, checks stationary XYZ via read-only APIs,
then writes ack-NNNN.json. No next snap occurs until the previous acknowledgement.
Replies are published by temporary-file rename. Errors produce error.json.
STOP cancels polling; each acknowledgement waits at most 20 seconds. Native
AcquireImage/Save calls themselves are synchronous, so STOP cannot interrupt
inside one. The client also owns a bounded RunJob RPC and sends StopJob on
timeout/interrupt for its own job, then unloads and closes its lease/channel.
It reports late job failures instead of treating image arrival as job success.
Manual stage motion >0.3 um aborts the stationary client.

Public interfaces used

Macro: Zen.Acquisition.ActiveCamera; IsLiveRunning; IsContinuousRunning;
AcquireImage(); camera.Name, ExposureTime, Binning, Frame;
GetAllParameterNames(), GetParameterValue(name); Zen.Application.Save(image,path);
image.Close(). No undocumented Python binding or native driver access.

Client: WorkflowService v3beta IsJobRunning, IsJobTemplateLoaded,
LoadJobTemplate, RunJob, StopJob, UnloadJobTemplate; StageService v2 GetPosition;
FocusService v2 GetPosition. These are the installed generated contracts.

JobResourcesService GetDoubleValue/SetDoubleValue and GetStringValue/SetStringValue
are official interfaces already used by the proven camera job. They operate
on LOADED JOB RESOURCES, not arbitrary camera state. This macro-only job does
not pretend those resource values constitute hardware camera readback.

Remaining uncertainty and intended comparison

Runtime camera parameter names and representations are unresolved until the
inventory trial. Macro registration, immediate-snap operation while a macro
job is running, cancellation timing, profile equivalence, raw export fidelity
and actual speed remain untested on hardware. The file transport intentionally
retains CZI export overhead. Its timing split will show whether amortizing
workflow purging (~0.54 s/job in saved logs) is beneficial before investing in
more complex persistent-stream correlation. This prototype is not suitable
for unattended production until the stationary comparison qualifies it.

Offline checks

  .venv\Scripts\python.exe -m pytest scripts/camera_fastpath/test_protocol.py -q
Tests use an in-memory fake Zen object and temporary files only. They check
exact request/file identity, disabled profile, wrong session, STOP and missing
parameter bindings. They do not establish vendor compatibility or performance.
