"""Daily reminder job. Runs as a Kubernetes CronJob using the same image."""
import core


def main():
    r = core.get_redis()
    checked = sent = 0
    for doc_id in r.smembers("docs:all"):
        doc = core.load_doc(r, doc_id)
        if not doc:
            r.srem("docs:all", doc_id)
            continue
        checked += 1
        remind = int(doc["remind_days"])
        days, _ = core.status_of(doc["expiry"], remind)
        stage = core.reminder_stage(days, remind)
        if stage is None or stage == doc.get("last_stage"):
            continue
        results = core.notify(doc, days)
        print(f"{doc['title']}: {days} days left, stage {stage}, results {results}")
        if any(results.values()):          # remember it only if someone was actually told
            r.hset(f"doc:{doc_id}", "last_stage", stage)
            sent += 1
    print(f"checked={checked} reminders_sent={sent}")


if __name__ == "__main__":
    main()
