import os
import json
import time
import csv
import io
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for
import requests

app = Flask(__name__)

ROCKETAPI_KEY = os.environ.get("ROCKETAPI_KEY", "")
MIN_FOLLOWERS = int(os.environ.get("MIN_FOLLOWERS", "100000"))
TC_INSTAGRAM_USER_ID = "244687506"  # @trueclassic

# In-memory job store (persists for process lifetime, enough for Render)
jobs = {}

def get_user_id(username: str, api_key: str) -> str:
    """Resolve Instagram username to user ID."""
    resp = requests.post(
        "https://v1.rocketapi.io/instagram/user/get_info",
        headers={"Authorization": f"Token {api_key}"},
        json={"username": username},
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data["response"]["body"]["data"]["user"]["id"])


def scrape_followers(job_id: str, user_id: str, api_key: str, min_followers: int):
    """Background thread: paginate through all followers, filter by min_followers."""
    job = jobs[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.utcnow().isoformat()
    job["results"] = []
    job["total_scanned"] = 0
    job["pages"] = 0
    job["errors"] = 0

    max_id = None
    consecutive_errors = 0

    while True:
        if job.get("cancelled"):
            job["status"] = "cancelled"
            return

        payload = {"id": user_id, "count": 50}
        if max_id:
            payload["max_id"] = max_id

        try:
            resp = requests.post(
                "https://v1.rocketapi.io/instagram/user/get_followers",
                headers={"Authorization": f"Token {api_key}"},
                json=payload,
                timeout=20
            )
            resp.raise_for_status()
            data = resp.json()
            consecutive_errors = 0
        except Exception as e:
            job["errors"] += 1
            consecutive_errors += 1
            job["last_error"] = str(e)
            if consecutive_errors >= 5:
                job["status"] = "error"
                job["error_message"] = f"Too many consecutive errors: {e}"
                return
            time.sleep(5)
            continue

        body = data.get("response", {}).get("body", {})
        users = body.get("users", [])

        if not users:
            break

        job["pages"] += 1
        job["total_scanned"] += len(users)

        for u in users:
            fc = u.get("follower_count", 0)
            if fc >= min_followers:
                job["results"].append({
                    "username": u.get("username", ""),
                    "full_name": u.get("full_name", ""),
                    "follower_count": fc,
                    "following_count": u.get("following_count", 0),
                    "is_verified": u.get("is_verified", False),
                    "is_private": u.get("is_private", False),
                    "biography": u.get("biography", ""),
                    "external_url": u.get("external_url", ""),
                    "profile_url": f"https://instagram.com/{u.get('username', '')}",
                })

        job["qualified_count"] = len(job["results"])

        # Check for next page
        next_max_id = body.get("next_max_id")
        if not next_max_id:
            break
        max_id = next_max_id

        # Polite delay to avoid rate limiting
        time.sleep(1.2)

    job["status"] = "complete"
    job["finished_at"] = datetime.utcnow().isoformat()
    # Sort results by follower count descending
    job["results"].sort(key=lambda x: x["follower_count"], reverse=True)


@app.route("/")
def index():
    return render_template("index.html", jobs=jobs, default_api_key=ROCKETAPI_KEY)


@app.route("/start", methods=["POST"])
def start_scan():
    api_key = request.form.get("api_key", "").strip()
    username = request.form.get("username", "trueclassic").strip().lstrip("@")
    min_followers_input = int(request.form.get("min_followers", MIN_FOLLOWERS))

    if not api_key:
        return jsonify({"error": "API key required"}), 400

    job_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    jobs[job_id] = {
        "id": job_id,
        "username": username,
        "min_followers": min_followers_input,
        "status": "starting",
        "results": [],
        "total_scanned": 0,
        "qualified_count": 0,
        "pages": 0,
        "errors": 0,
        "created_at": datetime.utcnow().isoformat(),
    }

    def run():
        try:
            # Resolve user ID if not using default TC
            if username.lower() == "trueclassic":
                uid = TC_INSTAGRAM_USER_ID
            else:
                uid = get_user_id(username, api_key)
            jobs[job_id]["user_id"] = uid
            scrape_followers(job_id, uid, api_key, min_followers_input)
        except Exception as e:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error_message"] = str(e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return redirect(url_for("job_status", job_id=job_id))


@app.route("/job/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return "Job not found", 404
    return render_template("job.html", job=job)


@app.route("/job/<job_id>/status.json")
def job_status_json(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    # Return summary without full results list for polling
    return jsonify({
        "id": job["id"],
        "status": job["status"],
        "total_scanned": job["total_scanned"],
        "qualified_count": job["qualified_count"],
        "pages": job["pages"],
        "errors": job.get("errors", 0),
        "last_error": job.get("last_error", ""),
    })


@app.route("/job/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    job = jobs.get(job_id)
    if job:
        job["cancelled"] = True
    return redirect(url_for("job_status", job_id=job_id))


@app.route("/job/<job_id>/download")
def download_csv(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("results"):
        return "No results", 404

    output = io.StringIO()
    fieldnames = ["username", "full_name", "follower_count", "following_count",
                  "is_verified", "is_private", "biography", "external_url", "profile_url"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(job["results"])

    filename = f"trueclassic_followers_{job_id}_{job['qualified_count']}accounts.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/jobs")
def all_jobs():
    return render_template("jobs.html", jobs=jobs)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5556))
    app.run(host="0.0.0.0", port=port, debug=False)
