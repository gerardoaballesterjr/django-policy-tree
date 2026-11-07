# Fields

Allow policies can limit fields:

- `readable_fields`: what the user sees. Empty means all.
- `writable_fields`: what the user may set on create or update. Empty means all.

A user gets the **union** of the matching allows. Deny policies can't list fields; they apply to whole records.

## Per record

Field rights are worked out **per record**. Someone who is a viewer in one region (`readable_fields: id, name, status`) and a manager of a team elsewhere (all fields) sees each row with the fields allowed where that row lives.

For lists, this is computed for all rows at once. It costs no queries when every matching rule grants the same fields, otherwise one query per rule, never one per row.

## In DRF

`PolicyFieldsMixin`:

- removes fields the user can't read and can't write;
- makes fields they can't write read-only, so submitted values are ignored;
- makes fields they can write but not read write-only;
- trims each row to the fields allowed for that row.

## In plain Django forms

`PolicyViewMixin` removes fields the user can neither read nor write and **disables** fields they can read but not write. Django ignores submitted values for disabled fields.

## Writable fields and creates

The field that places a new record (for example `organization`, or `project` for tasks that reach their unit through a project) must be writable on create policies, or left empty for "all fields". Otherwise the request can't name where the record goes.
