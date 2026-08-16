import {
  useDaemons,
  useDeleteTemplate,
  useScreens,
  useTemplates,
  type Screen,
} from "@/api/queries"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Landed } from "@/routes/templates/Landed"
import { TemplateCard } from "@/routes/templates/TemplateCard"

/**
 * The panels that name a template, in the order the server listed them.
 *
 * By **name**, because that is the only thing joining the two tables: a screen's
 * `template` column is a name, it has no foreign key, and a screen naming a
 * template that does not exist is a state the database allows and the snapshot
 * refuses. So this is a string match and not a lookup, and a template no panel
 * names comes back empty rather than missing.
 *
 * The screen order is `ORDER BY position, id`, taken as given -- the same order
 * the rack canvas draws and the same one `snapshot._screens` uses. It decides
 * which panel is previewed, so re-sorting here would preview a different panel
 * from the one the caption names on any rack where position and id disagree.
 */
function drawnBy(screens: Screen[], name: string): Screen[] {
  return screens.filter((each) => each.template === name)
}

/**
 * Every template the server holds, what it draws, and which panels draw it.
 *
 * **What this page does not do, said once and plainly.** It does not edit
 * `scenes` and it does not create templates. A template is a document of scenes
 * and the editor for those is phase 2; `NewTemplate` requires `scenes`, so a
 * create form here would have to invent a scene list nobody asked for or post an
 * empty one, and an amend that sent `scenes` back would write whatever this page
 * last read over whatever the visual editor writes later. So this lists what
 * there is, shows what it draws, decides which panel draws what, renames and
 * recategorises, and deletes the rows the server will let go of.
 *
 * **Why it asks for the racks.** Two reasons, both about naming: a panel is on a
 * rack and the card says which, and `X-Unservable-Daemons` answers in ids that
 * have to become names. `useDaemons` is the hook that fills the cache entry the
 * header strip reads, so this is one fetch on mount and no polling, exactly as
 * on the Screens page.
 */
export function TemplatesPage() {
  const templates = useTemplates()
  const screens = useScreens()
  const racks = useDaemons()
  // One delete for the whole list, above the cards: a delete that lands takes
  // its own card off the page, and a mutation living on that card would take
  // the answer -- including the racks that did not get it -- with it.
  const remove = useDeleteTemplate()

  const rackRows = racks.data ?? []
  const screenRows = screens.data ?? []
  const removed = remove.data

  return (
    <>
      <h1 className="text-2xl font-semibold">Templates</h1>
      <p className="max-w-prose text-sm text-muted-foreground">
        What a panel draws, and which panels draw it. The scenes inside a template are not edited
        here &mdash; that editor is phase 2 &mdash; so what this page changes is a template&rsquo;s
        name and category, and which panel is pointed at which.
      </p>

      {templates.isPending && (
        <p className="text-sm text-muted-foreground">Reading the templates&hellip;</p>
      )}
      {templates.isError && (
        <Alert variant="destructive">
          <AlertTitle>The templates could not be read</AlertTitle>
          <AlertDescription>{templates.error.message}</AlertDescription>
        </Alert>
      )}
      {/* The panels are a separate fetch, and every card is about them: which
          panel is previewed, which panels can be assigned, which can be
          detached. A page that only said the templates loaded would draw every
          card as "no panel draws it" while this request was failing. */}
      {screens.isError && (
        <Alert variant="destructive">
          <AlertTitle>The panels could not be read</AlertTitle>
          <AlertDescription>{screens.error.message}</AlertDescription>
        </Alert>
      )}
      {racks.isError && (
        <Alert variant="destructive">
          <AlertTitle>The racks could not be read</AlertTitle>
          <AlertDescription>{racks.error.message}</AlertDescription>
        </Alert>
      )}
      {templates.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          This server holds no templates. The built-ins are re-seeded at every start, so an empty
          list means the server has not finished starting or its template table was emptied by
          hand.
        </p>
      )}

      <Landed
        saved={removed}
        racks={rackRows}
        what={`${remove.variables?.name ?? "The template"} was deleted.`}
      />

      <div className="grid gap-4">
        {/* The server's order, `ORDER BY name`, and this page does not re-sort:
            sorting by id here would draw the list in the order the rows happen
            to have been written, which is neither alphabetical nor stable
            across a re-seed. */}
        {templates.data?.map((template) => (
          <TemplateCard
            key={template.id}
            template={template}
            templates={templates.data}
            screens={screenRows}
            drawing={drawnBy(screenRows, template.name)}
            racks={rackRows}
            remove={remove}
          />
        ))}
      </div>
    </>
  )
}
