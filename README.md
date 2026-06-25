# Event-Driven Serverless Order Processing System (EDSOPS)

A full-stack, event-driven serverless order processing system built entirely on **Microsoft Azure**, developed as a Work Integrated Project at **George Brown College**.

🎥 **[Watch the demo video](https://youtu.be/avMOhnBnhyY)**

---

## Overview

EDSOPS simulates a real-world e-commerce workflow — **LaptopZone**, an online laptop store. Customers place orders through a web frontend, and the entire backend runs serverlessly in the cloud with no always-on server.

When an order is placed, it flows through a chain of independent Azure Functions connected by Storage Queues: the order is received, validated, inventory is checked and updated, a confirmation email is sent, and the order is logged to a database — all asynchronously, so the customer receives an instant response while the heavy work happens in the background.

## Architecture