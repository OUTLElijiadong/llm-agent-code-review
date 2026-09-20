from flask import Flask, request
import sqlite3
import subprocess

app = Flask(__name__)

@app.get("/search")
def search():
    user_name = request.args.get("name", "")
    with sqlite3.connect(":memory:") as connection:
        return str(connection.execute("SELECT id FROM users WHERE name = '" + user_name + "'").fetchall())

@app.get("/ping")
def ping():
    host = request.args.get("host", "localhost")
    return subprocess.check_output("ping -c 1 " + host, shell=True).decode()

@app.post("/calculate")
def calculate():
    return str(eval(request.form["expression"]))
