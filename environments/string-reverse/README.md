# String-reverse environment

This package contains the task validation, prompt construction, answer
extraction, and shaped scorer used by the static three-character environment.
Task rows carry their payload as opaque JSON; all rows may point at one shared
guest image.

The guest asks the policy to respond with the reversed string in
`<answer>...</answer>` tags. The scorer uses the final well-formed answer tag,
trims surrounding whitespace, and compares its first three Unicode scalar
values with the target. Each matching position earns one reward point; every
character beyond the three-character target subtracts one point. Responses
without a valid answer tag receive zero reward.
