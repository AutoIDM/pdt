# pdt builds this file in this folder, as `docker build .` would, and runs the
# CMD at the end on every scheduled run.
FROM node:22-bookworm-slim
WORKDIR /app
COPY hello.js .
CMD ["node", "hello.js"]
