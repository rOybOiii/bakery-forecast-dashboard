# Bakery Forecast manager dashboard

A public-safe Streamlit demonstration using four synthetic restaurants. The
app reads a small, validated dashboard bundle plus an optional versioned
Credent backend snapshot. It never imports or executes the Forecast, PPRM,
JEAM, ERP, or MMM inference projects.

The first backend vertical slice is published under
`data/backend_snapshot_v2/`. It contains a checksum-verified published model
run, one Forecast 2 vintage, five ERP/PIPv4 event occurrences, and a
materialized event-family estimate. The estimate is immediately available in
the event-planning catalog as **ERP positive pulse**; no MCMC runs in Streamlit.
The coherent Forecast 2 vintage remains in standardized sales units because
the residual-only simulation did not pass through Forecast 1's real-sales
preprocessing. It is validated and retrievable but cannot replace the existing
dollar-denominated demo chart. Native-dollar publication now requires the exact
Forecast 1 inverse—its Box-Cox lambda, transformed mean and standard deviation,
fit cutoff, and source-data hash—and transforms every posterior draw before
calculating dashboard quantiles.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

Without hosted authentication settings, a local database may use the explicit
`local-streamlit-manager` development identity. This fallback is refused when
`CREDENT_ENV=production`.

## Identity and organization access

The manager application uses Streamlit's OpenID Connect boundary. Copy
`.streamlit/secrets.toml.example` to the untracked `secrets.toml` file and
configure the client with an OIDC identity provider. The provider owns signup,
passwords, email verification, sessions, and MFA. Credent stores the verified
provider subject, customer account, organization memberships, invitations, and
audit records; it never stores a customer password.

Only two MVP access levels are exposed:

- **System administrator** can service every organization. MFA must be required
  by the identity-provider policy for this account.
- **Organization administrator** can access and administer only organizations
  with an active membership.

Organization administrators can rename the organization and its Sites/Units,
add or disable administrators, and choose the permitted advertising channels
for each Site/Unit. A pending invitation is accepted only when the same
verified email signs in. Disabling preserves the account and audit history.

Credent now includes a hosted gateway boundary. Streamlit sends the provider's
short-lived ID token to that service; the gateway verifies it, enforces every
organization/site membership, performs administration, and owns those database
operations. Sales history, corrections, and forecast/recovery dispatch also use
that authenticated path. Forecast, Events, Marketing, onboarding, event-family,
imputation, reconciliation, and monitoring operations use an allowlisted,
site-scoped application API whose actor identity is derived by the gateway.
The Mac worker uses outbound requests with short-lived job-only credentials for
claims and verified artifact transfer. Neither hosted Streamlit nor the worker
needs a database password.

## Deploy

1. Push this directory to the root of its own GitHub repository.
2. In Streamlit Community Cloud, choose **Create app** and then
   **Yup, I have an app**.
3. Select that repository, the `main` branch, and `streamlit_app.py` as the
   entrypoint.
4. In **Advanced settings**, select Python 3.12 and configure the OIDC settings
   plus the HTTPS `CREDENT_API_URL`. Do not give hosted Streamlit a database
   password. Set `CREDENT_ENV=production` in the hosted environment.
5. Choose an optional `streamlit.app` subdomain and deploy.

Community Cloud installs the pinned packages in `requirements.txt` and reads
the synthetic bundle committed under `data/dashboard_bundle_v1/`.

Only synthetic data is included. Do not add real client sales or upstream
posterior artifacts to a public repository.

## Downtown marketing-plan MVP

The Downtown restaurant view is the first operational-forecast vertical slice.
It keeps one implemented marketing plan and at most one saved draft in
`data/marketing_plan_state_v1.json`. Unsaved drawer edits remain in Streamlit
session state, while the deterministic preview scorer stands in for the future
frozen-MMM scenario adapter. The other restaurant pages remain on the earlier
dashboard design during this qualification.

Plans can be optimized or edited manually as one sequence of learning-aware
spend blocks, using a four-day cadence by default. Each block carries daily
channel spend into the adstock and saturation preview, and managers can use an
explicit override when a block needs to exceed its recommended channel range.

## Downtown event-plan MVP

The forecast page also includes a front-end-only event planning drawer. It
offers a static searchable catalog, suggested events for the visible horizon,
category and morphology tags, recurring placement, forecast-copy duration and
magnitude edits, daily shape edits, and 90% effect ranges. Event scenarios can
be previewed or implemented on the operational forecast without altering the
historical event templates. Drafts and implemented event plans currently live
only in Streamlit session state; a durable database and event-review workspace
remain future work.
