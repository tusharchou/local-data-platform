# Base

The "base class" of a new Python library is a foundational component that sets the architectural standard for other classes within the library, especially when you anticipate multiple, varied implementations of a core concept.

Its primary purpose is to define a common interface and/or shared functionality that derived classes will inherit or adhere to.

Here's what should generally inform the design of a base class for a new Python library:

## What is the Core Concept/Abstraction?

What fundamental operation or entity does your library revolve around? (e.g., a Connector to different databases, a Parser for various file formats, a Strategy for different algorithms, a DataSource for different data origins). The base class should represent this abstraction.

## What are the Common Operations/Interface?

What actions or behaviors must any concrete implementation of this core concept provide? These will become your abstract methods.

What common utility methods or shared logic can be provided directly by the base class to avoid code duplication in derived classes? These will be your concrete methods.

### To Be Abstract or Not Abstract (ABCs):

If you want to enforce an interface: Use an Abstract Base Class (ABC) from the abc module. This is highly recommended when you want to ensure that any class inheriting from your base class must implement certain methods. If a concrete class fails to implement an @abstractmethod, Python will raise a TypeError upon instantiation.

Example Use Case: A StorageBase class with abstract get() and put() methods. Any new storage backend (e.g., S3Storage, FileSystemStorage) must provide these methods.

