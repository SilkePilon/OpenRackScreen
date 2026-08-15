import { useCachedDaemons } from "@/api/queries"
import { AppSidebar } from "@/components/app-sidebar"
import { Separator } from "@/components/ui/separator"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { cn } from "@/lib/utils"
import { LiveProvider } from "@/live/LiveProvider"

/**
 * One dot per rack, and the only place this interface says a Pi has gone.
 *
 * Read from the cache, never fetched -- `useCachedDaemons` subscribes to the
 * `daemons` query entry without a query function, so a header that is on every
 * page does not turn every page into one that asks the server for the rack
 * list. What fills it is `GET /api/daemons` on the pages that list racks, and
 * what keeps it true is the `daemons` message writing through `setQueryData` in
 * `LiveProvider`. Same entry, same rule, as the panels below: the strip and a
 * panel can never disagree about whether a rack is there, because there is only
 * one answer and neither of them computes it.
 *
 * A cache nobody has filled draws **no dots at all**. `undefined` is "this
 * interface has not been told", which is not "there are no racks" and is
 * certainly not "they are all gone" -- a strip that drew a row of dark dots
 * during the first second of a page load would be reporting an outage that had
 * not happened.
 *
 * The dots are in a list because that is what they are, and each carries its
 * own label rather than the strip carrying one for all of them: a dot is two
 * pixels of colour, and colour alone is not a thing a screen reader or a person
 * who cannot tell green from grey can read.
 */
function RackStrip() {
  const racks = useCachedDaemons()

  return (
    <ul aria-label="Rack status" data-testid="rack-strip" className="flex items-center gap-2">
      {racks?.map((rack) => (
        <li
          key={rack.id}
          // The whole of what a dot says, in the order a sentence says it, so
          // the accessible name is the status and not just the rack.
          aria-label={`${rack.name} is ${rack.online ? "online" : "offline"}`}
          title={rack.name}
          className={cn(
            "size-2.5 rounded-full transition-colors",
            rack.online ? "bg-emerald-500" : "bg-muted-foreground/40",
          )}
        />
      ))}
    </ul>
  )
}

/**
 * The authenticated chrome: the sidebar, the header, and the live connection.
 *
 * `LiveProvider` is here rather than above the router because this component is
 * exactly the set of pages that have a session. `/login` and `/setup` render
 * none of this, and a socket dialled from them would be a connection the server
 * refuses -- and then a client retrying it on a backoff behind a login form.
 * One shell on screen is one `/ws/ui`, for as long as somebody is signed in.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <LiveProvider>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4">
            <SidebarTrigger className="-ml-1" />
            <Separator orientation="vertical" className="mr-2 h-4" />
            <RackStrip />
          </header>
          <div className="flex flex-1 flex-col gap-4 p-4">{children}</div>
        </SidebarInset>
      </SidebarProvider>
    </LiveProvider>
  )
}
