# Product roadmap

## Current development

- Complete the first-data-import workflow for each Site / Unit.
- Exercise the controller locally with real database records and staged model runs.
- Replace simulated stages with qualified Credent Core adapters incrementally.
- Replace the current baseline controller-validation Forecast adapter with the
  production strict-cut Forecast after ERP publishes a calibrated PIPv4 event
  package; keep the baseline warning visible until that handoff is qualified.

## Shared development environment

- Move the development PostgreSQL database to a managed environment when local
  integration is stable.
- Run Streamlit against the shared development database.
- Add a remote worker workflow for claiming model jobs and publishing results.

## Controller and lineage extensions

- Add explicit cross-run artifact dependencies for jobs that consume outputs
  produced by earlier jobs.
- Introduce the more general controller-job queue when work extends beyond a
  single model run, including organization-wide MMM work and standalone family
  estimate refreshes.
- Record which upstream jobs and artifacts each queued job requires so data
  invalidation can cancel waiting descendants, request safe cancellation of
  running descendants, and invalidate already-published descendants.
- Add cross-job coordination only where it is genuinely required; continue to
  represent Detrend, PPRM, JEAM, Forecast, and publication as ordered steps of
  one model run.
- Show dependency-aware queue messages in Streamlit, including why a job is
  waiting, canceled, or superseded and which replacement job follows it.

### Provenance Change Recovery

Status: the conservative anchored-rebuild path is implemented locally. It uses
the last successful pre-change run only as an input-scope anchor, layers all
accepted dataset revisions through the correction, and executes the currently
active Forecast Refresh contracts. Model-state checkpoint replay remains a
separate, unimplemented qualification project.

For an enabled MMM program, recovery now performs a fresh full-history Learning
fit from the reconstructed data and confirmed spend. It does not seed the fit
with prior MMM parameters or daily score posteriors. An operational program
keeps the replacement forecast behind the publication barrier until the fresh
fit, marketing-clean baseline rebuild, and MMM Rescore publish together.

- Start Provenance Reconciliation only when accepted evidence is substantively
  revised. Identical overlapping uploads are no-ops; appended dates are normal
  updates, not provenance conflicts.
- Trace the changed rows through lineage and invalidate only their actual
  descendants. A corrected marketing-spend value, for example, should not
  invalidate unrelated event or organic-forecast work.
- [x] Record a versioned Provenance Reconciliation Manifest with the input-anchor
  run, anchor dataset, intervening accepted revisions, corrected dates and
  values, replacement input, invalidation, current-contract recovery run, and
  visible rebuilt-forecast annotation.
- [x] Keep the prior published package in history and let the existing
  publication barrier control when the rebuilt forecast and MMM Rescore become
  current.
- [x] When MMM is enabled, bootstrap fresh Learning from the reconstructed
  history, exclude prior fit and score estimates, rebuild history with the new
  fit, and publish the new fit, baseline, and Rescore as one recovery package.
- Define and scientifically qualify an optimized recomputation contract for every model
  before enabling replay. Candidate policies include checkpointed online PPRM,
  a bounded JEAM revision window, the Forecaster's configured training window,
  and an MMM-specific fit and carryover window. These are design candidates,
  not approved production rules.
- Preserve the last safe unaffected published package where available while
  replacements run, and prevent queued or running work with stale lineage from
  publishing.
- Reconcile manager-owned events separately from model inference: preserve
  interpretations, tags, families, and splits when old provenance is retired;
  reconnect only strong redetections automatically and send ambiguous splits
  or merges to manager review.
- Qualify replay against full clean rebuilds, including parameter estimates,
  event detection, forecast output, uncertainty, and attribution, before any
  replay path can replace production results.
- Provide an audit view showing the changed evidence, selected replay boundary,
  affected descendants, retained results, replacement jobs, and old-to-new
  provenance links.

### Cross-run regime ownership and baseline rebasing

- Quantify the risk that repeated Forecast and MMM refits gradually absorb
  persistent PPRM regimes into flexible baseline components.
- Persist comparable daily component attribution for every published run:
  baseline, PPRM regime, JEAM / ERP event, MMM marketing, and unexplained
  residual.
- Compare successive runs and flag ownership drift when a regime contribution
  shrinks while the baseline grows over the same dates, or when MMM begins
  claiming variation already assigned to a frozen regime or event.
- Keep MMM conditioned on frozen non-marketing components so it cannot silently
  reclassify PPRM regimes or JEAM / ERP events as marketing response.
- Shadow-test each candidate baseline rebase against the current
  baseline-plus-regime model on rolling holdouts. Compare forecast accuracy,
  interval calibration, residual structure, and attribution stability.
- Make rebasing an explicit, versioned promotion: preserve the old lineage,
  record which regime was promoted into the new baseline, and never silently
  rewrite or delete prior results.
- Begin with manager-reviewed rebase proposals. Automate promotion only after
  retrospective and synthetic qualification establish conservative thresholds
  and an acceptable false-promotion rate.

### Candidate Credent job types

Use this catalog to guide the general job contract and workflow registry. It is
a planning reference, not a requirement to implement every job immediately.

#### Forecasting and model updates

1. **First forecast / full onboarding fit** — Preflight, Detrend, batch PPRM,
   JEAM, ERP, optional MMM, Forecast, validation, and publication.
2. **Single-site daily forecast update** — Approved observations, online PPRM,
   recent-window JEAM, Forecast, validation, and publication.
3. **Multi-site corroborated forecast update** — Coordinated site inputs,
   panel JEAM Pass 1, site-level Pass 2, and site forecasts.
4. **Scheduled full model refit** — Refit state-space, batch PPRM, JEAM, ERP,
   and forecasts from a defined data cutoff.
5. **Online PPRM state update** — Advance saved online state using newly
   approved observations.
6. **MMM refit** — Scheduled or manually requested marketing-model update,
   potentially spanning multiple sites.
7. **Forecast refresh after MMM** — Rebuild affected forecasts using the newly
   published marketing response.

#### Events and planning

8. **Event-family estimate refresh** — Triggered when an event is added to,
   removed from, or reclassified within a family.
9. **Planned-event or scenario scoring** — Evaluate a proposed future event
   and its adjusted daily effects.
10. **Marketing-plan scoring** — Score a spend plan using the current forecast
    and learned marketing response.

#### Data and recovery

11. **Data correction reprocessing** — Rebuild affected results after approved
    observations or cleansing decisions change.
12. **Replacement-dataset rebuild** — Invalidate old lineage and create new
    model outputs from the replacement source.
13. **Historical backfill** — Process newly supplied older observations without
    treating them as an ordinary daily update.
14. **Failed or interrupted job recovery** — Resume from a safe checkpoint or
    restart according to the frozen workflow's retry policy.

#### Qualification and system work

15. **Shadow-model run** — Run batch and online PPRM, or current and candidate
    models, without publishing the candidate.
16. **Model qualification run** — Execute larger diagnostics and promotion
    gates before a model version becomes active.
17. **Artifact migration or verification** — Copy artifacts to durable object
    storage and verify checksums and lineage.
18. **Baseline rebase qualification** — Shadow the current
    baseline-plus-regime model against a candidate rebased model, measure
    cross-run attribution drift and holdout performance, and propose a
    versioned promotion without publishing it automatically.

Preflight, diagnostics, validation, publication, cancellation, and invalidation
are normally shared workflow steps or controller capabilities rather than
independent job types.

## Required before production launch

### Identity and tenant authorization

- [x] Add verified OIDC identity synchronization without storing passwords.
- [x] Enforce system-administrator and organization-administrator boundaries in
  organization selection and administration.
- [x] Add audited organization/site naming, administrator invitation and
  disablement, and per-site advertising-channel controls.
- [ ] Configure the hosted identity provider, verified-email policy, invitation
  delivery, and mandatory MFA policy for system administrators.
- [x] Add the hosted gateway, Auth0 token verification, server-side tenant
  authorization, audited identity/organization administration, and short-lived
  job-only worker credentials.
- [x] Move operational sales history, update/correction submission, Forecast
  Refresh/Recovery dispatch, remote job claiming, and digest-verified worker
  artifact delivery behind the gateway. The outbound Mac worker no longer
  needs database credentials.
- [x] Move onboarding, Forecast, Events, Marketing, event reconciliation,
  imputation, family refresh, and job-status reads/actions behind an allowlisted
  site-scoped gateway API. Hosted Streamlit now runs without a database
  credential.
- [ ] Qualify tenant-isolation and disabled-membership tests against the hosted
  authentication and backend deployment.

### Durable object storage

Replace machine-local upload and artifact directories with private,
S3-compatible object storage before any production deployment.

Scope:

- [x] Add one storage abstraction used by onboarding, the controller, and model
  publishers, with a local-filesystem implementation.
- [ ] Add and qualify the remote-object-storage implementation.
- Store original customer uploads, approved/prepared input snapshots, model
  artifacts, diagnostics, and published outputs as immutable objects.
- Keep object metadata and lineage in PostgreSQL: object key, checksum, size,
  media type, environment, owning organization/site, and related dataset,
  review, or model run.
- Use separate development and production buckets and credentials.
- Keep buckets private and use narrowly scoped service credentials or temporary
  signed access where needed.
- Migrate existing local artifacts and verify every copied object by checksum.
- Test retry safety, interrupted uploads, missing objects, retention, backup,
  and recovery before launch.
- Prevent production startup when configured to use machine-local durable
  storage.

Current preferred hosted implementation: Cloudflare R2 through its
S3-compatible API. The storage interface must remain provider-neutral so the
provider can be changed without changing model or application logic.

### Production readiness gate

Production launch is blocked until durable object storage is installed,
migrated, secured, and exercised end to end by both Streamlit and the model
worker.
