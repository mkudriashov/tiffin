"""The commit this tree was exported from. `git archive` fills the placeholder in (see
.gitattributes, `export-subst`); a checkout or a wheel built from one keeps it as written."""

COMMIT = "$Format:%H$"
