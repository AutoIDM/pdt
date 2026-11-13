# Concepts

A **project** is a directory holding `pdt.yml`. It has one or more **apps** in subdirectories. An **app** is what pdt runs or deploys.

An **app** is a **scheduled job** (**job** for short) or a **web app**. A **job** has a `run.py` or `run.ps1` that defines what it does. A **web app** has a `run.py` that runs an http server.

pdt chooses how to deploy your app based on what kind of app it is, its settings, and where you're deploying it. The kind of app is its **profile**. pdt reads `app_profile` from pdt.yml to see what kind it is. It could be `job` or `web`. `job` is the default.
