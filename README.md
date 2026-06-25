# Event-Driven Serverless Order Processing System (EDSOPS)

A full-stack, event-driven, serverless order processing system built entirely on **Microsoft Azure**, developed as a Work Integrated Project at **George Brown College**.

🎥 **Demo Video:** https://youtu.be/avMOhnBnhyY

---

## Table of Contents
- [Overview](#overview)
- [Problem Statement](#problem-statement)
- [System Architecture](#system-architecture)
- [How It Works](#how-it-works)
- [The Five Azure Functions](#the-five-azure-functions)
- [Key Features](#key-features)
- [Azure Services Used](#azure-services-used)
- [Data Storage](#data-storage)
- [Email Notifications](#email-notifications)
- [Security](#security)
- [Monitoring & Observability](#monitoring--observability)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Deployment](#deployment)
- [Architecture Principles](#architecture-principles-demonstrated)

---

## Overview

EDSOPS simulates a real-world e-commerce workflow — **LaptopZone**, an online laptop store. Customers browse a catalogue of laptops, place an order through a web form, and receive an automated email confirmation. The entire backend runs serverlessly in the cloud with **no always-on server**.

Instead of one large server handling everything in sequence, each task — receiving the order, validating it, managing inventory, sending email, and saving to a database — is handled by a small, independent **Azure Function** that activates only when needed. Functions communicate through **Storage Queues**, making the system fully decoupled and asynchronous: the customer gets an instant response while heavy background work happens behind the scenes.

---

## Problem Statement

Traditional order systems require a constantly running server that processes every task synchronously — the customer waits while the server validates the order, sends an email, and writes to a database before responding. This is slow under load, expensive to keep running 24/7, and fragile, since a failure in one step can break the

---

## How It Works

**Stage 1 — Frontend.** The customer selects a laptop on the website (hosted on Azure Static Web Apps), enters their details and quantity, and clicks "Place Order." JavaScript packages the order as JSON and sends an HTTP POST to the backend.

**Stage 2 — Entry point.** `submit_order` performs lightweight Tier 1 checks (valid JSON, required fields present), assigns a unique order ID and timestamp, drops the order onto the `orders-incoming` queue, and instantly returns "Order received."

**Stage 3 — Validation & inventory.** `validate_order` triggers off the queue and runs deeper Tier 2 business validation: email format, quantity, product existence, and live stock availability. It decrements the laptop's stock if the order is valid, or routes invalid/out-of-stock orders to the `orders-invalid` dead-letter queue.

**Stage 4 — Parallel processing.** Valid orders are fanned out to two queues simultaneously: one triggers `send_confirmation_email` (emails the customer), the other triggers `log_to_table` (saves the order to the database). These run in parallel and independently.

**Stage 5 — Monitoring.** Every function emits telemetry to Application Insights, which stitches each order's full journey into a single end-to-end transaction trace.

---

## The Five Azure Functions

| Function | Trigger | Responsibility |
|---|---|---|
| `submit_order` | HTTP | Tier 1 validation, generate order ID, enqueue order, return instant response |
| `validate_order` | Queue | Tier 2 validation, inventory check + decrement, fan-out or reject |
| `send_confirmation_email` | Queue | Send styled HTML confirmation email to the customer |
| `send_rejection_email` | Queue | Send styled rejection email when an order fails (e.g. out of stock) |
| `log_to_table` | Queue | Persist the completed order to Azure Table Storage |

All functions are written in **Python** using the **Azure Functions v2 programming model** in a single `function_app.py`.

---

## Key Features

- **Two-Tier Validation** — fast structural checks at the edge (`submit_order`) for an instant response, plus deeper business validation in the background (`validate_order`).
- **Live Inventory Management** — each laptop has a real stock count that decrements with every order (e.g. 10 → 9 → 8 …). Orders exceeding available stock are rejected.
- **Fan-Out Pattern** — a valid order is copied into two queues simultaneously so email and database logging run in parallel.
- **Dead-Letter Queue** — invalid or out-of-stock orders are routed to a separate queue for handling, never lost.
- **Customer Confirmation Emails** — professionally styled HTML emails sent on successful orders.
- **Out-of-Stock / Rejection Emails** — customers are notified with the reason their order could not be processed.
- **Low-Stock Alerts** — the store owner is automatically emailed to restock when a laptop's inventory runs low (including when it reaches zero).
- **Secret Management** — the email service connection string is stored in **Azure Key Vault** and accessed via the Function App's **managed identity**, not in plain text.
- **End-to-End Observability** — distributed tracing across all functions via Application Insights.

---

## Azure Services Used

| Service | Role in the System |
|---|---|
| **Azure Static Web Apps** | Hosts the HTML/CSS/JS frontend; auto-deployed from GitHub |
| **Azure Functions** (Consumption Plan, Python v2) | The five serverless functions that process orders |
| **Azure Storage Queues** | Decouple the functions — `orders-incoming`, `orders-to-email`, `orders-to-log`, `orders-invalid` |
| **Azure Table Storage** | `Orders` audit table + `LaptopInventory` stock table (NoSQL) |
| **Azure Communication Services (Email)** | Sends confirmation, rejection, and low-stock alert emails |
| **Azure Key Vault** | Securely stores the ACS connection string secret |
| **Application Insights** | Collects logs, metrics, and end-to-end transaction traces |
| **Azure Monitor** | Unified metrics, alerts, and dashboards |
| **Log Analytics Workspace** | Centralized log storage and KQL queries |

---

## Data Storage

**Orders Table** (audit log of every confirmed order):
- `PartitionKey` = order date (YYYY-MM-DD)
- `RowKey` = unique order UUID
- Fields: CustomerName, CustomerEmail, Product, Quantity, UnitPrice, SubmittedAt, ValidatedAt, Status

**LaptopInventory Table** (live stock tracking):
- `PartitionKey` = "LAPTOP"
- `RowKey` = product SKU (e.g. `HP-PAV-15`)
- Fields: Name, Brand, Price, Stock

---

## Email Notifications

The system sends three distinct styled HTML emails through Azure Communication Services:

1. **Order Confirmation** — sent to the customer when an order succeeds (order number, product, quantity, price, total).
2. **Order Rejection** — sent to the customer when an order fails validation or is out of stock, with the reason clearly shown.
3. **Low-Stock Alert** — sent to the store owner/admin when a laptop's stock falls to a defined threshold, prompting a restock.

---

## Security

- The **ACS connection string** is stored as a secret in **Azure Key Vault**.
- The Function App is assigned a **system-assigned managed identity**.
- That identity is granted the **"Key Vault Secrets User"** role (read-only access to secrets).
- The app setting references the secret via a **Key Vault reference**, so no secret is stored in plain text in the app configuration.
- **CORS** is configured on the Function App so only the web frontend can call it from the browser.

---

## Monitoring & Observability

- **Application Insights** auto-instruments all functions — no extra code required.
- **Transaction Search** stitches each order's journey (HTTP request → queues → email → database) into a single trace.
- **Performance** views show execution counts and durations per function (revealing, for example, that email sending is the slowest step — exactly why it runs asynchronously off a queue).
- **Live Metrics** show function executions in real time.

---

## Tech Stack

- **Backend:** Python (Azure Functions v2 programming model)
- **Frontend:** HTML, CSS, JavaScript (vanilla)
- **Cloud Platform:** Microsoft Azure
- **Backend Deployment:** Azure Functions Core Tools (`func azure functionapp publish`)
- **Frontend Deployment:** GitHub → Azure Static Web Apps (automatic CI/CD)
- **Tools:** Visual Studio Code, Azure CLI, Git

---

---

## Deployment

### Backend (Azure Functions)
```bash
func azure functionapp publish <function-app-name>
```
Application settings (ACS sender address, admin email, and Key Vault reference for the ACS connection string) are configured on the Function App in Azure.

### Frontend (Azure Static Web Apps)
The website is pushed to this GitHub repository. Azure Static Web Apps is connected to the repo and automatically rebuilds and redeploys the site whenever changes are pushed.

---

## Architecture Principles Demonstrated

- **Event-Driven Architecture** — the system reacts to events (messages on queues) rather than polling.
- **Serverless Computing** — no servers to manage; scales to zero when idle (pay only per execution).
- **Decoupled Design** — each component is fully independent; a failure in one never breaks the others.
- **Scalability & Resilience** — automatically handles one order or thousands; durable queues ensure no order is lost.
- **Observability** — full end-to-end distributed tracing and monitoring.

---

**George Brown College — Work Integrated Project**
**Cloud Provider: Microsoft Azure**