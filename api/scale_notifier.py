"""Scale notifier: tells Slack whenever the autoscaler adds or removes API pods.
Runs as its own small Deployment, using the same image as the API."""
import json, os, ssl, time, urllib.parse, urllib.request

import core

API = os.getenv("KUBE_API") or "https://{}:{}".format(
    os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
    os.getenv("KUBERNETES_SERVICE_PORT", "443"))
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NS = os.getenv("NAMESPACE", "default")
NAME = os.getenv("TARGET_NAME", "renewguard-api")
POLL = float(os.getenv("POLL_SECONDS", "5"))      # how often to look
SETTLE = float(os.getenv("SETTLE_SECONDS", "10"))  # wait for new pods to appear


def kube_get(path):
    headers, ctx = {}, None
    if API.startswith("https"):
        with open(f"{SA}/token") as f:
            headers["Authorization"] = "Bearer " + f.read().strip()
        ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    req = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        return json.load(r)


def get_replicas():
    d = kube_get(f"/apis/apps/v1/namespaces/{NS}/deployments/{NAME}")
    return d["spec"].get("replicas", 1)


def get_pods():
    sel = urllib.parse.quote(f"app={NAME}")
    items = kube_get(f"/api/v1/namespaces/{NS}/pods?labelSelector={sel}")["items"]
    return {p["metadata"]["name"] for p in items
            if not p["metadata"].get("deletionTimestamp")
            and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")}


def get_hpa():
    """Returns (current cpu %, target cpu %, min pods, max pods)."""
    h = kube_get(f"/apis/autoscaling/v2/namespaces/{NS}/horizontalpodautoscalers/{NAME}")
    cur = target = None
    for m in h["spec"].get("metrics", []):
        if m.get("type") == "Resource" and m["resource"]["name"] == "cpu":
            target = m["resource"]["target"].get("averageUtilization")
    for m in h.get("status", {}).get("currentMetrics") or []:
        if m.get("type") == "Resource" and m["resource"]["name"] == "cpu":
            cur = m["resource"]["current"].get("averageUtilization")
    return cur, target, h["spec"].get("minReplicas"), h["spec"]["maxReplicas"]


def autoscaler_reason(new_size):
    """The autoscaler's own explanation, taken from its SuccessfulRescale event."""
    sel = urllib.parse.quote(f"involvedObject.name={NAME},reason=SuccessfulRescale")
    items = kube_get(f"/api/v1/namespaces/{NS}/events?fieldSelector={sel}")["items"]
    items.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime")
               or e["metadata"]["creationTimestamp"])
    for e in reversed(items):
        if f"New size: {new_size};" in e.get("message", ""):
            return e["message"].split("reason:", 1)[-1].strip()
    return None


def build_message(old, new, before, after, hpa, why):
    cur, target, lo, hi = hpa
    up = new > old
    n = abs(new - old)
    s = "" if n == 1 else "s"
    if up:
        head = f":arrow_up: *RenewGuard scaled UP* from {old} to {new} pods ({n} pod{s} created)"
        reason = (f"CPU is at {cur}% of the requested CPU, above the {target}% target, "
                  f"so the autoscaler added {n} pod{s}." if cur is not None and target else
                  f"The autoscaler added {n} pod{s} because CPU was above its target.")
    else:
        head = f":arrow_down: *RenewGuard scaled DOWN* from {old} to {new} pods ({n} pod{s} removed)"
        reason = (f"CPU is at {cur}% of the requested CPU, below the {target}% target, "
                  f"so the autoscaler removed {n} pod{s}." if cur is not None and target else
                  f"The autoscaler removed {n} pod{s} because CPU was below its target.")
    if why and ("MinReplicas" in why or "MaxReplicas" in why):   # e.g. first deploy
        reason = f"The autoscaler moved the pod count to keep it between {lo} and {hi} pods."
    lines = [head, f"Reason: {reason}"]
    if why:
        lines.append(f"Autoscaler says: {why}")
    total = len(after) if len(after) == new else f"{len(after)} (target {new}, still starting)"
    lines.append(f"Total pods now: {total}. Limits: minimum {lo}, maximum {hi}")
    created, removed = sorted(after - before), sorted(before - after)
    if up and created:
        lines.append("New pods: " + ", ".join(created))
    if not up and removed:
        lines.append("Stopped pods: " + ", ".join(removed))
    return "\n".join(lines)


def check_once(state, hook):
    """Look once. If the replica count changed, tell Slack. Returns the message or None."""
    replicas, pods = get_replicas(), get_pods()
    message = None
    if state["replicas"] is not None and replicas != state["replicas"]:
        time.sleep(SETTLE)                       # give the new pods a moment to appear
        pods_after = get_pods()
        try:
            why = autoscaler_reason(replicas)
        except Exception:
            why = None
        message = build_message(state["replicas"], replicas, state["pods"], pods_after,
                                get_hpa(), why)
        print(message, flush=True)
        if hook:
            core._post_slack(hook, message)
        pods = pods_after
    state["replicas"], state["pods"] = replicas, pods
    return message


def main():
    hook = os.getenv("SLACK_WEBHOOK_URL")
    state = {"replicas": None, "pods": set()}
    print(f"watching {NAME} in namespace {NS}; Slack {'on' if hook else 'OFF'}", flush=True)
    while True:
        try:
            check_once(state, hook)
        except Exception as e:                    # keep running through API hiccups
            print("error:", e, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
