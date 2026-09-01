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
