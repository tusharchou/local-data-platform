from local_data_platform import helloworld
from local_data_platform.entity import User, Product
from local_data_platform.transaction import Transaction 



'''
	Product? Uber 
	User? Rider & Driver
	Handshake?
	_Booking_
		*Event_id*
		Rider :+1 Uber
		Uber :? Driver
		Driver :+1 Uber
		Uber :+1 Rider
'''
class Booking(Transaction):
	
	def __init__(self, product: Uber, provider: Driver, consumer: Rider):
		self.product = product
		self.provider = provider
		self.consumer = consumer

	def get():
		pass

	def put():
		pass
class Uber(Product):


class Rider(User):


class Driver(User):


helloworld()
