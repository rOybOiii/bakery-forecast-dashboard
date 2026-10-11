# Bakery Forecast manager dashboard

A public-safe Streamlit demonstration using four synthetic restaurants. The
app reads a small, validated dashboard bundle and never imports or executes the
Forecast, CPD, JEAM, or ERP inference projects.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Deploy

1. Push this directory to the root of its own GitHub repository.
2. In Streamlit Community Cloud, choose **Create app** and then
   **Yup, I have an app**.
3. Select that repository, the `main` branch, and `streamlit_app.py` as the
   entrypoint.
4. In **Advanced settings**, select Python 3.12. This demonstration requires
   no secrets.
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
