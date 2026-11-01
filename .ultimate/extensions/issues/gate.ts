/**
 * The project's own intake gate, shared by both facets so the button and the prompt cannot disagree: `is_authorized`
 * in `src/tactus/domain/work_source.py` lets an issue be started only when it carries `fleet:ready` and neither
 * `fleet:blocked` nor `fleet:human`; an open issue or milestone alone is not permission.
 */

/** The fleet labels of an issue, without their prefix, in the order they came. */
export const fleetLabels = (labels: readonly string[]) =>
  labels
    .filter((label) => label.startsWith('fleet:'))
    .map((label) => label.slice('fleet:'.length))

/** Whether the project's gate allows this issue's work to be started. */
export const authorized = (labels: readonly string[]) => {
  const fleet = fleetLabels(labels)
  return fleet.includes('ready') && !fleet.includes('blocked') && !fleet.includes('human')
}
