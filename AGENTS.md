# Minmax
Minmax will be an app that tracks data from my life. Minmax will be a hub for storing, viewing, and processing said data. It will have mechanisms for reliable storage, retrieval, and analysis of said data.

## Goals
The tasks you should actively be working on will be listed in `TODO.md`. You should complete your tasks with the understanding that the goals listed below are the endpoint, but you should not try and complete them immediately. The following goals for a rough timeline.
- Minmax will support efficient import, storage, and retrieval for timetracking data.
- Minmax will include a convenient user interface for viewing timetracking data.
- Minmax will include a correlation engine that analyzes timetracking data, and provides the user with recommendations.
- Minmax will support efficient import, storage, and retrieval for time-bound datastreams beyond timetracking.
- Minmax's user interface will include a timeline that allows the user to see trends over time in as many datastreams as they choose.
- Minmax's correlation engine will support cross-stream correlations.
- Minmax will allow the user to make queries in the form of a desire to increase or decrease some statistics, and it will provide advice in the form of other statistics to increase or decrease.
- Minmax's correlation engine will include a custom DL model that predicts future data based on the past.

## Invariants
If the goals are the function we're looking to optimize, these are the constraints we're looking to optimize it under. They must not be violated
### Development Practices
Minmax should be a robust, maintainable codebase. This means that whenever you finish a prompt, you should assume that it will be the last time you touch the project. After that, someone else must be able to take over and pick up where you left off without issue. This means
- Current tasks will be stored in `TODO.md`. Top level tasks will be created by humans only, but you may add subtasks as you see fit.
You must write rigorous documentation for everything. This includes both inline documentation of code, but also documentation of features in `README.md`.
- You must keep incomplete subtasks properly logged in `TODO.md`.
- You must write rigorous tests to ensure safety of the data trusted to Minmax and a smooth user experience.
- The user interface must remain primarily separate from the backend. Redoing the UI should have no effect on the underlying systems.
- Both work completed and work to be completed should be represented in `TODO.md`. This means that before beginning work on a prompt, you should add any relevant subtasks you've been instructed to complete to `TODO.md`. If there is no reasonable top-level task to put these under, ask the user to create one.