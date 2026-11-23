# Editor's Desk

Your whole book as one page you type in, with notes from your editor in the margin, and behind it a publishing house's stages of editing, big to small. Manuscript keeps the book, Maps pictures it, and Editor's Desk does the editing. Your editor works in the background with your bot's brain on this computer, and only when you say so. Your bot never writes your book.

## PuffPAI

### Write

The page opens on **Write**: every chapter in order on one page, like a Word document. Click anywhere and type. Enter starts a new paragraph, Ctrl+Z and Ctrl+Y undo and redo, Ctrl+I and Ctrl+B make italic and bold. Paste comes in as plain text, keeping italic and bold.

- **It saves as you type**, about two seconds after you stop, and when you leave the page. Ctrl+S saves at once. The line at the top always says where things stand: "Saved 3:42", "Saving...", or, in red, what went wrong. Your words stay on the page until they are saved.
- **Earlier versions** sit under each chapter heading. While you type, a version is kept at most every ten minutes; Ctrl+S keeps one every time; the text before you ever changed it is always kept.
- **Chapters** are listed on the left with their word counts. Click one to go there.
- **Find** (Ctrl+F) searches the whole book, and can replace one place or all of them.
- **Notes in the margin.** Each open finding is marked on its words, with its note beside them: what is wrong, why it matters, one way to fix it, and Fixed, Not a problem or Later. The Notes button hides them all.
- **Save a Word copy** makes an ordinary Word file of your book as it stands, to read or share, in My files, in the Editor's Desk folder under your book's title. Your book is not changed.
- **Ask your editor.** Select some words and press Ask your editor. Your editor reads them with the paragraphs around them, says what works and what does not, and why. Your editor never writes sentences for you.

If the same chapter changed somewhere else while you typed, nothing is lost: a bar asks whether to keep yours (the other goes to Earlier versions) or show the other so you can copy from it.

Italic and bold are kept in the section files as `*italic*`, `**bold**` and `***both***`. Your editor and every check read the words without the stars.

### The Editor's desk

The **Editor's desk** view leads with your editor's letter: what the book is, what's working, the biggest problems in order, and what to do first. Under it is the next thing to do and why, then five stages, each a row you open:

1. **The story.** Plot and plot holes; Structure and pacing; Characters; Point of view; Opening and ending; Themes and stakes.
2. **The facts.** Continuity; Science and world rules; Real-world facts (it only flags what to look up).
3. **The prose.** Voice; Dialogue; Prose habits.
4. **The polish.** Grammar and syntax; Style sheet; Proofread.
5. **Getting it out.** Reader's eye; Sensitive content and permissions; Length and genre fit; Synopsis and query letter; Submission format.

At any time: **Future plans and changes**. Type "What if Elias survives?" and it traces what that touches and lists what you'd need to revisit. The **revision plan** in the letter keeps up as you mark things.

Each finding says what the problem is, where (its section and exact quote: click to see it in Write), why it matters, and one way to fix it. Mark it Open, Fixed, Not a problem or Later, with a note of your own. A re-run doesn't bring back what you marked Not a problem, and says if something you marked Fixed still looks broken.

Your uploaded original is never touched. **Use this** (proposed) puts a grammar or proofreading correction in, on your click only, after showing you the before and after.

A check is long. It shows what it is doing, how far along it is and about how long is left, from real timings, and it carries on through page closes and restarts. It waits while Manuscript reads. It never starts by itself: it starts when you press it, or when you say yes in chat.

### Study a book

Put a book you admire beside your own. On the Manuscript page, press **Add a book to study**: it goes on the shelf without being read into a story bible. In **Study a book**, pick it. The numbers show at once, side by side: where the two books differ most, where you already match, and chapter by chapter, how long the sentences run and how much is people talking. Different is not wrong. Then, if you like, type what you most want to learn from it and press **Ask your editor to compare passages**. Your editor reads matched pairs of short passages, one of theirs and one of yours (how the book opens, people talking, a slower stretch, a fast one, how a chapter closes, right after a scene break), in the background like a check, and gives you what to try first and notes on each pair, with the words they rest on marked. "Go to this in your book" opens Write at your passage. Your editor never rewrites your sentences.

### What it needs

Manuscript's folder of your books, and for most checks a story bible, so let Manuscript read the book first. Prose habits, Grammar, Style sheet, Proofread, Length and genre fit and Submission format run without one. No setup steps, no internet, and no outside packages.

### Adding a check

Add a text file to `checks/`. Its head says its name, its stage, what it does in plain words, what it reads and how far to trust it. Each `## block` is an ask: `thread`, `character`, `section`, `theme`, `claim`, `speakers`, `opening`, `ending`, `book` or `change` is asked once for each of those; `judge` weighs what code found (`find:` names the code); `write <name>` drafts a document.

### Tools for chat

`desk_checks`, `desk_run` (needs `confirm` true, after her yes), `desk_status`, `desk_findings`, `desk_mark`, `desk_letter`, `desk_ask`, `desk_documents`, `desk_stop`, `desk_study` (needs `confirm` true, after her yes) and `desk_study_report`. No tool writes your text.

### Looking at it without PuffPAI

`python fixtures/run_local.py --port 8765` copies the test novel to a temp folder, adds some italics and a few notes, puts Night Ferry beside it as a book to study, and serves the page at the address it prints, with a stand-in brain that always gives the same answer, and for Study a book, notes that quote the passages it was sent.

Cypher Sisters. puff puff pass.
