# How Notes finds a note, ticks a checklist, and reads files in

## A note file

1. Each note is one file, `notes/<title words>.md`, in the data folder. A second note with the same title gets `-2`, `-3` and so on.
2. The file starts with a front matter block between two `---` lines: id, title, created, updated, tags, pinned and notebook. The note's text follows.
3. A file with no front matter is still a note. Its title is its first `# Heading` line, or else its file name. Its dates are the file's own date.
4. A file that is empty, larger than 1024 KB, not UTF-8 text, or has a front matter block that never ends, is listed as "would not read" and is never written over.
5. The list of notes is built again from the files whenever a file in the folder changes, so files copied in or edited by hand show up within a few seconds.
6. Saving a note writes a new file beside it and then puts it in place in one step. An existing file is written only when it still reads as the same note.
7. Saving rewrites only Notes' own keys. Every other front matter key, with the lines under it, is kept exactly as written and in its order, above Notes' keys.
8. A file is read whatever its line endings, and saved with the line endings it had.
9. A file whose id is not valid, or the same as a file earlier in name order, gets a new id made from its file name. The page says so once. The new id is written into the file the next time the note is saved; the file keeps its name.

## Finding a note by its title (every tool that takes a note)

1. The same id or title, in any case.
2. The same title once words like "the", "my" and "note" are left out ("the shopping note" finds "Shopping").
3. One title that holds the words.
4. One title that has every word.
5. One note whose title, tags or text has every word.
6. At any step, more than one: nothing changes, and the answer lists them. None at all: nothing changes, and the answer lists the newest notes.

## Checklists

1. A line that starts with `- [ ]` is a tick box; `- [x]` is a ticked one. Spaces before the dash are kept.
2. On the page, ticking a box changes that one line in the file. If the line changed since the page read it, nothing changes and the page reads the note again.
3. `tick_item` finds the item by its words: the same words first, then one item that holds them, then one that has every word. Several or none: nothing changes, and the answer lists the items.
4. `add_to_note` adds a new tick box when the note already has tick boxes, and a plain line otherwise, unless told which.

## Search

1. Every word must be found in the title, the tags or the text. A word matches the start of a word in the note, so "tomato" finds "tomatoes".
2. A tag filter needs that exact tag. A notebook filter takes the same name first, then one that holds the words.
3. Results are pinned notes first, then the most recently changed.

## Import

1. A zip, or one `.md`, `.markdown` or `.txt` file.
2. A zip is refused as a whole if it holds more than 2000 files, says it would unpack to more than 1024 MB, or holds a name that starts with /, holds a \\, a : (as in a drive letter), .. or a zero byte. The upload is at most 200 MB.
3. Each Markdown or text file in it becomes a new note. Other files, folders, and names that start with a dot are passed over. A link is skipped and counted as skipped.
4. The bytes really unpacked are counted as they are read. At 1024 MB import stops, and the answer says which files were not imported.
5. Front matter is read when it is there, and keys Notes does not use are kept. Otherwise the first `# Heading` line becomes the title and is taken out of the text, or the file name is the title.
6. A note whose front matter id is already here is skipped, so importing an export twice adds nothing. Import never writes over a note: a new file name is found for each note.
7. A file that will not read is skipped and named in the answer.

## Export

1. One zip with every note's file under `notes/`, exactly as it is on disk.
