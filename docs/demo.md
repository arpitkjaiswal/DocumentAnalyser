# A three-minute project walkthrough

## Start with the working product

Run the application, choose **Try a sample document**, and submit the suggested question. Click an inline citation and compare the answer with the passage. Export the session to show that evidence is included in the downloaded Markdown.

Upload another document, select it in **Search in**, and ask a question that distinguishes it from the sample. Explain that document scope filters retrieval candidates; it is not just an instruction sent to a language model.

## Show reliability, not just the happy path

1. Re-upload the same filename with changed content. It replaces the document rather than accumulating duplicates.
2. Try an empty or invalid file. The API rejects it without destroying a valid existing document.
3. Stop and restart the server. The library reloads from SQLite. Explain why this needs a persistent disk on ephemeral hosting.
4. Ask a zero-overlap question, such as “cryptocurrency blockchain”, against the sample. The app returns no matching evidence instead of citing an arbitrary passage.

A question with overlapping words can still retrieve irrelevant evidence. Demonstrate that boundary honestly rather than claiming the retriever understands every question.

## Make the design explainable

Open `docs/architecture.md` and discuss three decisions:

- Why TF-IDF is a useful inexpensive baseline, and how you would compare it with dense or hybrid retrieval before adding infrastructure.
- How validation, a SQLite transaction, and cache replacement keep failed uploads from damaging the active library.
- Why this implementation runs with one worker, and what cache invalidation and tenant ownership would be needed for multiple users and instances.

Run `python -m unittest discover -s tests -v` and `python -m evaluation.run`. Distinguish the tiny synthetic retrieval smoke fixture from a real benchmark. Do not present its scores as general answer accuracy.

Describe the original source and your own changes accurately. Be ready to explain a specific implementation decision or failure you investigated; that is more useful than a claim that the project guarantees a hiring result.
