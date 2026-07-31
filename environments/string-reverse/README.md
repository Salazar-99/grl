# String-reverse environment

This package contains the task validation, neutral prompt, normalization, and
shaped scorer used by the static three-character environment. Task rows carry
their payload as opaque JSON; all rows may point at one shared guest image.

The scorer compares the first three Unicode scalar values after trimming
surrounding whitespace. Formatting validity is reported separately, so short,
long, and explanatory answers can still receive positional partial credit.
