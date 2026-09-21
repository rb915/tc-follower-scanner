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

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
MIN_FOLLOWERS = int(os.environ.get("MIN_FOLLOWERS", "100000"))
ACTOR_ID = "apify~instagram-follower-scraper"

# In-memory job store
jobs = {}


def run_apify_scan(job_id: str, username: str, results_limit: int, min_followers: int, token: str):
    """Start Apify actor run, poll until done, filter results."""
    job = jobs[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.utcnow().isoformat()

    try:
        # Start the actor run
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "usernames": [username],
                "resultsLimit": results_limit,
                "getFollowers": True,
                "getFollowing": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        run_data = resp.json()["data"]
        run_id = run_data["id"]
        dataset_id = run_data["defaultDatasetId"]
        job["run_id"] = run_id
        job["dataset_id"] = dataset_id
        job["apify_url"] = f"https://console.apify.com/actors/runs/{run_id}"

    except Exception as e:
        job["status"] = "error"
        job["error_message"] = f"Failed to start Apify run: {e}"
        return

    # Poll until complete
    while True:
        if job.get("cancelled"):
            # Abort the Apify run
            try:
                requests.post(
                    f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs/{run_id}/abort",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
            except Exception:
                pass
            job["status"] = "cancelled"
            return

        try:
            poll = requests.get(
                f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs/{run_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            poll.raise_for_status()
            run_info = poll.json()["data"]
            status = run_info["status"]
            job["apify_status"] = status

            # Update live stats from run stats
            stats = run_info.get("stats", {})
            job["total_scanned"] = stats.get("outputItemCount", 0)

            if status == "SUCCEEDED":
                break
            elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                job["status"] = "error"
                job["error_message"] = f"Apify run ended with status: {status}"
                return

        except Exception as e:
            job["errors"] = job.get("errors", 0) + 1
            job["last_error"] = str(e)

        time.sleep(5)

    # Fetch results
    try:
        offset = 0
        page_size = 1000
        all_results = []

        while True:
            r = requests.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                headers={"Authorization": f"Bearer {token}"},
                params={"format": "json", "limit": page_size, "offset": offset},
                timeout=30,
            )
            r.raise_for_status()
            items = r.json()
            if not items:
                break
            all_results.extend(items)
            if len(items) < page_size:
                break
            offset += page_size

        job["total_scanned"] = len(all_results)

        # Filter by min_followers
        qualified = [
            {
                "username": item.get("username", ""),
                "full_name": item.get("fullName", ""),
                "follower_count": item.get("followersCount", 0),
                "following_count": item.get("followingCount", 0),
                "is_verified": item.get("isVerified", False),
                "is_private": item.get("isPrivate", False),
                "biography": item.get("biography", ""),
                "external_url": item.get("externalUrl", ""),
                "profile_url": f"https://instagram.com/{item.get('username', '')}",
            }
            for item in all_results
            if item.get("followersCount", 0) >= min_followers
        ]

        qualified.sort(key=lambda x: x["follower_count"], reverse=True)
        job["results"] = qualified
        job["qualified_count"] = len(qualified)
        job["status"] = "complete"
        job["finished_at"] = datetime.utcnow().isoformat()

    except Exception as e:
        job["status"] = "error"
        job["error_message"] = f"Failed to fetch results: {e}"


@app.route("/")
def index():
    return render_template("index.html", jobs=jobs, default_api_key=APIFY_TOKEN)


@app.route("/start", methods=["POST"])
def start_scan():
    token = request.form.get("api_key", "").strip()
    username = request.form.get("username", "trueclassic").strip().lstrip("@")
    min_followers_input = int(request.form.get("min_followers", MIN_FOLLOWERS))
    results_limit = int(request.form.get("results_limit", 500000))

    if not token:
        return jsonify({"error": "Apify API token required"}), 400

    job_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    jobs[job_id] = {
        "id": job_id,
        "username": username,
        "min_followers": min_followers_input,
        "results_limit": results_limit,
        "status": "starting",
        "results": [],
        "total_scanned": 0,
        "qualified_count": 0,
        "errors": 0,
        "created_at": datetime.utcnow().isoformat(),
    }

    t = threading.Thread(
        target=run_apify_scan,
        args=(job_id, username, results_limit, min_followers_input, token),
        daemon=True,
    )
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
    return jsonify({
        "id": job["id"],
        "status": job["status"],
        "apify_status": job.get("apify_status", ""),
        "total_scanned": job["total_scanned"],
        "qualified_count": job["qualified_count"],
        "errors": job.get("errors", 0),
        "last_error": job.get("last_error", ""),
        "apify_url": job.get("apify_url", ""),
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
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/jobs")
def all_jobs():
    return render_template("jobs.html", jobs=jobs)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5556))
    app.run(host="0.0.0.0", port=port, debug=False)
