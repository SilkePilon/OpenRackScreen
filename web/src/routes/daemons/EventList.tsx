import { useId } from "react"

import { useEvents } from "@/api/queries"

/**
 * What just happened to one rack, newest first.
 *
 * **Recent, and labelled recent.** `daemon_event` is a ring of 200 rows per
 * rack, trimmed on every insert, and a rack that keeps dropping and coming back
 * spends two of them on every reconnect -- a connect and a disconnect. So a
 * rack that has been flapping for an afternoon has thrown away everything from
 * the morning, and an interface calling this "history" would be promising
 * something the table cannot keep. The sentence under the heading says the same
 * thing to the person reading it, because the difference matters exactly when
 * somebody is looking for the event that explains a fault and it is no longer
 * there.
 *
 * For the same reason nothing here is remembered across a refetch: what was on
 * screen a minute ago is not evidence that it is still in the ring, and a list
 * that merged the two would show a row the server would no longer answer with.
 */
export function EventList({ daemonId }: { daemonId: number }) {
  const headingId = useId()
  const events = useEvents(daemonId)

  return (
    <section aria-labelledby={headingId} className="grid gap-1.5 border-t pt-3">
      <h3 id={headingId} className="text-sm font-medium">
        Recent events
      </h3>
      <p className="text-xs text-muted-foreground">
        A rack keeps only its last 200 events, and one that keeps dropping and coming back
        spends two of them on every reconnect, so something that was here a minute ago may
        already be gone.
      </p>

      {events.isPending && (
        <p className="text-sm text-muted-foreground">Reading this rack&rsquo;s events&hellip;</p>
      )}
      {events.isError && (
        <p className="text-sm text-destructive">
          {`This rack's events could not be read: ${events.error.message}`}
        </p>
      )}
      {events.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">Nothing has been recorded for this rack.</p>
      )}
      {events.data !== undefined && events.data.length > 0 && (
        <ol className="grid gap-1 text-sm">
          {events.data.map((event) => (
            <li key={event.id} className="flex flex-wrap items-baseline gap-x-2">
              {/* The timestamp as the server wrote it. Not reformatted: the row
                  is read next to a server log and a daemon log, and the one
                  thing that makes those three line up is that all of them say
                  the same UTC instant the same way. */}
              <time dateTime={event.at} className="text-xs tabular-nums text-muted-foreground">
                {event.at}
              </time>
              <span className="text-xs text-muted-foreground">{event.kind}</span>
              <span className={event.level === "info" ? undefined : "text-amber-600 dark:text-amber-500"}>
                {event.message}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
