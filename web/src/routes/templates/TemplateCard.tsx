import { useId } from "react"

import {
  useAmendTemplate,
  useDeleteTemplate,
  useSaveScreen,
  type Daemon,
  type Screen,
  type Template,
} from "@/api/queries"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { AmendTemplateDialog } from "@/routes/templates/AmendTemplateDialog"
import { AssignPanelDialog } from "@/routes/templates/AssignPanelDialog"
import { DeleteTemplateDialog } from "@/routes/templates/DeleteTemplateDialog"
import { DetachPanelDialog } from "@/routes/templates/DetachPanelDialog"
import { Landed } from "@/components/Landed"

/**
 * How big the still is drawn, in CSS pixels.
 *
 * The route renders at the panel's real size -- `PREVIEW_SIZE` on the server is
 * 240, "so the preview is the picture, not an impression" -- and this asks the
 * browser for the same number, so nothing is upscaled and the space is reserved
 * before the bytes arrive.
 */
const PREVIEW_SIZE = 240

/**
 * The still, drawn through a panel that names this template.
 *
 * **`preview` is a screen route, not a template route**, and that is a fact
 * about what a render needs rather than an omission: `_render` binds the
 * *screen's* params over the template's declared defaults, so a picture of a
 * template alone would be a picture of no panel in particular. So the card
 * previews through the first panel that names it, in the server's own
 * `position, id` order, and says which panel that was -- two panels drawing one
 * template can be showing quite different parameters, and the caption is the
 * difference between a preview and a claim about all of them.
 *
 * A template no panel names has nothing to render against, and the route would
 * answer 404 for a screen that is not there. The card says so in words rather
 * than asking for a picture that cannot be drawn -- but **only once the panels
 * have actually been read**. `GET /api/templates` and `GET /api/screens` are
 * independent, so "no panel draws it yet" is a definite claim about a list this
 * card may not have; a fetch in flight and a fetch that failed both look exactly
 * like a screen list with nothing in it. Unread, it says nothing, and the page
 * says instead that the panels are being read or could not be.
 *
 * **Nothing here turns the picture.** `rotation` and `hflip` describe how the
 * glass is bolted into the rack -- all four of the user's are 270 -- and the
 * render is made *before* the mount correction, exactly as the daemon streams a
 * live frame before applying it. So this is already what a person standing at
 * the rack sees: no transform on the image, and none on anything wrapped around
 * it. Rotating again would be wrong twice.
 */
function Preview({
  template,
  drawing,
  panelsKnown,
  rackName,
}: {
  template: Template
  /** The panel to draw it through, or `undefined` because no panel names it. */
  drawing: Screen | undefined
  /** Whether `GET /api/screens` has actually answered. */
  panelsKnown: boolean
  rackName: (daemonId: number) => string
}) {
  if (drawing === undefined) {
    if (!panelsKnown) return null
    return (
      <p className="max-w-prose text-sm text-muted-foreground">
        No panel draws it yet, and the preview is rendered for a panel &mdash; assign one and the
        picture appears here.
      </p>
    )
  }
  return (
    <figure className="grid justify-items-start gap-2">
      <img
        src={`/api/screens/${drawing.id}/preview`}
        width={PREVIEW_SIZE}
        height={PREVIEW_SIZE}
        alt={`${template.name} rendered for ${drawing.name}`}
        className="rounded-full bg-black ring-1 ring-foreground/10"
      />
      <figcaption className="max-w-prose text-xs text-muted-foreground">
        {`Rendered for ${drawing.name} on ${rackName(drawing.daemon_id)}, with no live data at ` +
          "all, so what shows is what the template itself declares. It is drawn the way the rack " +
          "draws it and before the panel's rotation and flip are applied — those describe " +
          "how the glass is bolted in, and this already shows what you would see standing in " +
          "front of it."}
      </figcaption>
    </figure>
  )
}

/**
 * One template: what it is, what it draws, who draws it, and what may be done to it.
 *
 * **The assign and the detach are one mutation.** Both are `PATCH
 * /api/screens/{id}` carrying `{template}` and nothing else, both make the same
 * query stale, and both go through `useSaveScreen` -- which is also what the
 * Screens inspector writes through, so there is no second assign path with a
 * different set of invalidations. It is mounted here rather than inside each
 * dialog so that what the write answered outlives the dialog closing over it.
 *
 * **A built-in is offered an amendment and not a delete.** The server refuses
 * the delete with 409 -- it is re-seeded at every start, so a delete that undoes
 * itself overnight is worse than one that refuses -- and a button that always
 * fails is a button that lies. The amendment is genuinely offered, because
 * `seed_builtin_templates` inserts `ON CONFLICT DO NOTHING` so an edit to a
 * built-in survives the next start, and the card says which of the two applies.
 */
export function TemplateCard({
  template,
  templates,
  screens,
  drawing,
  panelsKnown,
  racks,
  remove,
}: {
  template: Template
  /** Every template, for the detach dialog: a panel must name one of the others. */
  templates: Template[]
  /** Every panel, for the assign dialog. */
  screens: Screen[]
  /** The panels that name this template, in the server's order. */
  drawing: Screen[]
  /**
   * Whether `GET /api/screens` has answered.
   *
   * Everything this card says about panels is a claim about that list, and
   * `screens.data ?? []` cannot tell "no panels" from "not yet" or "the request
   * failed". So the sentence about no panel drawing it, and the control that
   * offers panels to assign, both wait for it.
   */
  panelsKnown: boolean
  racks: Daemon[]
  /**
   * The page's delete, passed down rather than mounted here.
   *
   * A delete that lands takes this card off the page before anything could be
   * drawn about it, so the answer -- including which racks did not get it -- has
   * to be held somewhere that outlives the row.
   */
  remove: ReturnType<typeof useDeleteTemplate>
}) {
  const headingId = useId()
  const patch = useSaveScreen()
  const amend = useAmendTemplate(template.id)
  const assigned = patch.data
  const amended = amend.data

  const rackName = (daemonId: number) =>
    racks.find((rack) => rack.id === daemonId)?.name ?? `rack ${daemonId}`

  const scenes = template.scenes.length
  const others = templates.filter((each) => each.name !== template.name)
  const candidates = screens.filter((each) => each.template !== template.name)

  return (
    <Card role="region" aria-labelledby={headingId} className="gap-3">
      <CardHeader className="gap-1">
        <h2 id={headingId} className="text-base font-medium">
          {template.name}
        </h2>
        <p className="text-sm text-muted-foreground">
          {`${template.builtin ? "Built-in" : "Yours"} · ${template.category} · ${scenes} ${
            scenes === 1 ? "scene" : "scenes"
          }`}
        </p>
      </CardHeader>

      <CardContent className="grid gap-3">
        {template.builtin && (
          <p className="max-w-prose text-sm text-muted-foreground">
            Built-in templates are re-seeded at every start, so this one cannot be deleted &mdash;
            it would be back overnight. It can be amended, and the amendment survives, because the
            re-seed inserts only what is missing. To stop a panel drawing it, detach the panel.
          </p>
        )}

        {patch.isError && (
          <p role="alert" className="text-sm text-destructive">
            {patch.error.message}
          </p>
        )}
        <Landed
          saved={assigned}
          racks={racks}
          what={
            assigned?.body === undefined
              ? "Saved."
              : `${assigned.body.name} now draws ${assigned.body.template}.`
          }
        />
        <Landed
          saved={amended}
          racks={racks}
          what={
            amended?.body === undefined ? "Saved." : `${amended.body.name} was saved on every rack.`
          }
        />

        <Preview
          template={template}
          drawing={drawing[0]}
          panelsKnown={panelsKnown}
          rackName={rackName}
        />

        {/* No list at all for a template nothing draws, rather than an empty
            labelled one: "Panels drawing ring-gauge" with nothing under it is a
            landmark that leads a screen reader nowhere, and the preview above
            has already said which state this is. */}
        {drawing.length > 0 && (
          <ul aria-label={`Panels drawing ${template.name}`} className="grid gap-2">
            {drawing.map((each) => (
              <li key={each.id} className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm">{`${each.name} on ${rackName(each.daemon_id)}`}</p>
                <DetachPanelDialog screen={each} template={template} others={others} patch={patch} />
              </li>
            ))}
          </ul>
        )}
      </CardContent>

      <CardFooter className="flex flex-wrap gap-2">
        {/* Only once the panels have been read. Unread, every panel there is
            filters out of `candidates`, so this would open on an empty select
            -- an assign control offering nothing, on a rack wall that is fully
            configured, with the reason a page away. */}
        {panelsKnown && (
          <AssignPanelDialog
            template={template}
            candidates={candidates}
            rackName={rackName}
            patch={patch}
          />
        )}
        <AmendTemplateDialog template={template} amend={amend} />
        {!template.builtin && <DeleteTemplateDialog template={template} remove={remove} />}
      </CardFooter>
    </Card>
  )
}
