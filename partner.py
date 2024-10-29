"""
Product Requirements Doc

Product? 
- Users are people who want to find and learn new recipes 
- Recipe is an explanation of how a dish is made. It has:
 	- ingredients: quantity, ingredient name 
  	- process
   	- prep time: minutes
    	- cook time: minutes
     	- difficulty: easy, intermediate, hard
      	- course: starter, main course, dessert, beverage
       	- Price: Rupees
- Offline Dinners
	- Register: Must be signed in to attend event 
 	- Dinner Name
  	- Cuisine
   	- Chef Name
    	- Menu: All dishes available for dinner
 	- Price: Rupees
"""
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
