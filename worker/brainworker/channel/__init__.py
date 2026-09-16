"""Reading a YouTube channel: which videos are worth paying to index, and what
the ones already indexed say together.

Three passes, in increasing order of what they cost and of how much they know:

* :mod:`~brainworker.channel.preselect` reads titles and descriptions. Cheap,
  and it is a **hypothesis about relevance and never proof** — a preacher's
  title says what a video is called, not what it argues.
* :mod:`~brainworker.channel.topics` reads the *uncorrected* transcript, which
  is free to obtain wherever the video has captions, and is therefore the first
  pass that has read the words. It is what stands between a title and a bill.
* :mod:`~brainworker.channel.synthesis` answers a question over the videos that
  were indexed, with every finding tied to a citation the code verified.
"""
