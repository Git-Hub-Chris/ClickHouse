import datetime
import os
import subprocess

from flask import Flask, flash, redirect, request, url_for


def run_command(command, wait=False):
    print("{} - execute shell command:{}".format(datetime.datetime.now(), command))
    lines = []
    p = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    if wait:
        for l in iter(p.stdout.readline, b""):
            lines.append(l)
        p.poll()
        return (lines, p.returncode)
    else:
        return (iter(p.stdout.readline, b""), 0)


UPLOAD_FOLDER = "./"
ALLOWED_EXTENSIONS = {"txt", "sh"}
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


@app.route("/")
def hello_world():
    return "Hello World"


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/upload", methods=["GET", "POST"])
def upload_file():
    if request.method == "POST":
        # check if the post request has the file part
        if "file" not in request.files:
            flash("No file part")
            return redirect(request.url)
        file = request.files["file"]
        # If the user does not select a file, the browser submits an
        # empty file without a filename.
        if file.filename == "":
            flash("No selected file")
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = file.filename
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            return redirect(url_for("upload_file", name=filename))
    return """
    <!doctype html>
    <title>Upload new File</title>
    <h1>Upload new File</h1>
    <form method=post enctype=multipart/form-data>
      <input type=file name=file>
      <input type=submit value=Upload>
    </form>
    """


@app.route("/run", methods=["GET", "POST"])
def parse_request():
    ALLOWED_COMMANDS = {
        "list_files": ["ls", "-l"],
        "show_date": ["date"]
    }
    data = request.data.decode("utf-8").strip()  # Decode and sanitize input
    if data in ALLOWED_COMMANDS:
        command = ALLOWED_COMMANDS[data]
        run_command(command, wait=True)
        return "Command executed successfully"
    else:
        return "Invalid command", 400


if __name__ == "__main__":
    app.run(port=5011)
